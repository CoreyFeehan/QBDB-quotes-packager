import streamlit as st
import pandas as pd
import pyodbc
import itertools
import math
import warnings
from shippo import Shippo
from shippo.models import components
from config import SHIPPO_API_KEY

warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy")

# -----------------------------
# CONFIG
# -----------------------------

MAX_BOX_WEIGHT = 40

shippo_client = Shippo(api_key_header=SHIPPO_API_KEY)

SHIP_FROM = {
    "name": "Warehouse",
    "street1": "Your Street",
    "city": "Your City",
    "state": "Your State Abbreviation",
    "zip": "Your Zip",
    "country": "US"
}

# -----------------------------
# LOAD DATA
# -----------------------------

def load_items():

    df = pd.read_csv("item_dimensions.csv", engine = "python")

    numeric = ["Weight","Length","Width","Height","UOM","ShipAloneQty"]

    for c in numeric:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    df["UOM"] = df["UOM"].replace(0,1)

    return df

def load_boxes():

    df = pd.read_csv("available_boxes.csv", engine = "python")

    df["Length"] = pd.to_numeric(df["Length"])
    df["Width"] = pd.to_numeric(df["Width"])
    df["Height"] = pd.to_numeric(df["Height"])

    df["Volume"] = df["Length"] * df["Width"] * df["Height"]

    return df.sort_values("Volume")

items_df = load_items()
boxes_df = load_boxes()

# -----------------------------
# QUICKBOOKS CONNECTION
# -----------------------------

def qb_conn():

    return pyodbc.connect(
        "DSN=QuickBooks Data;",
        autocommit=True
    )


def get_estimate_header(quote):
    conn=None
    try:
        conn = qb_conn()
        query=f"""
        SELECT
        TxnID,
        ShipAddressAddr1 AS Street,
        ShipAddressCity AS City,
        ShipAddressState AS State,
        ShipAddressPostalCode AS Zip
        FROM Estimate
        WHERE RefNumber='{quote}'
        """
        df=pd.read_sql(query,conn)
        return df
    finally:
        if conn:
            conn.close()

def get_estimate_lines(txn):
    conn=None
    try:
        conn=qb_conn()
        query=f"""
        SELECT
        EstimateLineItemRefFullName AS Item,
        EstimateLineQuantity AS Qty
        FROM EstimateLine
        WHERE TxnID='{txn}'
        """
        df=pd.read_sql(query,conn)
        return df
    finally:
        if conn:
            conn.close()

# -----------------------------
# ROTATIONS
# -----------------------------

def rotations(l,w,h):

    return list(set(itertools.permutations([l,w,h],3)))

# -----------------------------
# PACK ITEMS
# -----------------------------

def pack_items(lines):

    merged = lines.merge(items_df, on="Item", how="left")

    ignore=["shipping"]

    merged = merged[~merged["Item"].str.lower().isin(ignore)]
    merged = merged[(merged["Qty"].notna()) & (merged["Qty"] > 0)]

    # -----------------------------
    # FIND ITEMS NOT IN DIMENSION FILE
    # -----------------------------

    missing_items = merged[merged["Length"].isna()]["Item"].unique().tolist()

    # Remove them from packing
    merged = merged[merged["Length"].notna()]

    expanded = []

    for r in merged.itertuples():

        qty = int(r.Qty)
        weight = r.Weight
        dims = (r.Length, r.Width, r.Height)

        uom = int(r.UOM) if not pd.isna(r.UOM) and r.UOM > 0 else 1
        ship_alone = int(r.ShipAloneQty) if not pd.isna(r.ShipAloneQty) else 0

        if ship_alone > 0:

            full = qty // ship_alone
            remainder = qty % ship_alone

            for _ in range(full):

                expanded.append({
                    "item": r.Item,
                    "qty": ship_alone,
                    "weight": ship_alone * weight,
                    "dims": dims,
                    "alone": True
                })

            qty = remainder


        if qty > 0:

            groups = qty // uom
            remainder = qty % uom

            for _ in range(groups):

                expanded.append({
                    "item": r.Item,
                    "qty": uom,
                    "weight": uom * weight,
                    "dims": dims,
                    "alone": False
                })

            if remainder > 0:

                expanded.append({
                    "item": r.Item,
                    "qty": remainder,
                    "weight": remainder * weight,
                    "dims": dims,
                    "alone": False
                })


    # existing packing algorithm continues here
    # (no change required)

    expanded.sort(key=lambda x: x["weight"], reverse=True)

    parcels = []

    for item in expanded:

        if item["alone"]:

            parcels.append({
                "items":[item],
                "weight":item["weight"],
                "dims":[item["dims"]]
            })

            continue

        placed = False

        for p in parcels:

            if all(not it["alone"] for it in p["items"]) and p["weight"] + item["weight"] <= MAX_BOX_WEIGHT:

                p["items"].append(item)
                p["weight"] += item["weight"]
                p["dims"].append(item["dims"])

                placed = True
                break

        if not placed:

            parcels.append({
                "items":[item],
                "weight":item["weight"],
                "dims":[item["dims"]]
            })


    # box assignment logic continues (same as before)

    results = []

    for p in parcels:

        lengths = [d[0] for d in p["dims"]]
        widths = [d[1] for d in p["dims"]]
        heights = [d[2] for d in p["dims"]]

        L = max(lengths)
        W = max(widths)
        H = max(heights)

        box_found = None

        for _,box in boxes_df.iterrows():

            if L <= box.Length and W <= box.Width and H <= box.Height:

                box_found = box
                break

        if box_found is None:
            box_found = boxes_df.iloc[-1]

        results.append({
            "Box":box_found.BoxName,
            "BoxDims":(box_found.Length,box_found.Width,box_found.Height),
            "Weight":round(p["weight"],2),
            "Items":p["items"]
        })


    return results, missing_items

# -----------------------------
# SHIPPO
# -----------------------------

def create_shippo_parcels(parcels):

    shippo_parcels = []

    for p in parcels:

        L, W, H = p["BoxDims"]

        parcel = components.ParcelCreateRequest(
            length=str(L),
            width=str(W),
            height=str(H),
            distance_unit=components.DistanceUnitEnum.IN,
            weight=str(p["Weight"]),
            mass_unit=components.WeightUnitEnum.LB
        )

        shippo_parcels.append(parcel)

    return shippo_parcels

def get_fedex_rates(address, parcels):

    address_from = components.AddressCreateRequest(
        name="Warehouse",
        street1=SHIP_FROM["street1"],
        city=SHIP_FROM["city"],
        state=SHIP_FROM["state"],
        zip=SHIP_FROM["zip"],
        country="US"
    )

    address_to = components.AddressCreateRequest(
        name="Customer",
        street1=address["street1"],
        city=address["city"],
        state=address["state"],
        zip=address["zip"],
        country="US"
    )

    shipment_request = components.ShipmentCreateRequest(
        address_from=address_from,
        address_to=address_to,
        parcels=parcels
    )

    shipment = shippo_client.shipments.create(shipment_request)

    rates = shipment.rates

    fedex_rates = [r for r in rates if r.provider == "FedEx"]

    return fedex_rates


# -----------------------------
# STREAMLIT UI
# -----------------------------

st.title("📦 Shipping Box Planner")

st.markdown("Enter a QuickBooks quote number to calculate shipping boxes and rates.")

with st.form("quote_form"):

    quote = st.text_input("QuickBooks Quote Number")

    submitted = st.form_submit_button("Calculate Boxes")

if not submitted:
    st.stop()


# -----------------------------
# VALIDATE INPUT
# -----------------------------

if not quote:
    st.error("Please enter a quote number")
    st.stop()


# -----------------------------
# QUERY QUICKBOOKS
# -----------------------------

with st.spinner("Querying QuickBooks..."):

    header = get_estimate_header(quote)

if header.empty:

    st.error("Quote not found in QuickBooks")
    st.stop()

txn = header["TxnID"].iloc[0]


with st.spinner("Loading line items..."):

    lines = get_estimate_lines(txn)


# -----------------------------
# PACK ITEMS
# -----------------------------

with st.spinner("Calculating optimal boxes..."):

    parcels, missing_items = pack_items(lines)


# -----------------------------
# SHIP TO ADDRESS
# -----------------------------

st.subheader("📍 Ship To")

st.dataframe(
    header[["Street","City","State","Zip"]],
    width='stretch'
)


# -----------------------------
# SHOW MISSING ITEMS
# -----------------------------

if missing_items:

    st.warning("⚠ Items missing from item_dimensions.csv")

    missing_df = pd.DataFrame({
        "Missing Items": missing_items
    })

    st.dataframe(missing_df, width='stretch')


# -----------------------------
# SHOW BOX RESULTS
# -----------------------------

st.subheader("📦 Boxes")

shippo_parcels = []

for i, p in enumerate(parcels, 1):

    with st.container():

        st.markdown(f"### Box {i}")

        col1, col2 = st.columns(2)

        with col1:

            st.write("**Box Type:**", p["Box"])
            st.write("**Weight:**", p["Weight"], "lbs")

        with col2:

            # Combine duplicate items
            item_counts = {}

            for it in p["Items"]:
                item_counts[it["item"]] = item_counts.get(it["item"],0) + it["qty"]

            st.write("**Contents**")

            for item,qty in item_counts.items():
                st.write(f"- {item} x {qty}")

        # create shippo parcel
        shippo_parcels.append({
            "length": p["BoxDims"][0],
            "width": p["BoxDims"][1],
            "height": p["BoxDims"][2],
            "distance_unit": "in",
            "weight": p["Weight"],
            "mass_unit": "lb"
        })


# -----------------------------
# GET FEDEX RATES
# -----------------------------

if shippo_parcels:

    st.subheader("🚚 FedEx Shipping Rates")

    # Make sure the address keys match get_fedex_rates
    address = {
        "street1": header["Street"].iloc[0],
        "city": header["City"].iloc[0],
        "state": header["State"].iloc[0],
        "zip": header["Zip"].iloc[0],
        "country": "US"
    }

    with st.spinner("Getting live FedEx rates from Shippo..."):

        rates = get_fedex_rates(address, shippo_parcels)

    if rates:

        # Display only service, amount, and estimated days
        for r in rates:
            service = r.servicelevel.name
            cost = r.amount
            days = r.estimated_days if r.estimated_days else "N/A"
            st.write(f"**{service}** — ${cost} — {days} day(s)")

    else:
        st.warning("No FedEx rates returned")
