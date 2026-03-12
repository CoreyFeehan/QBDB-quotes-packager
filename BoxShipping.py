import streamlit as st
import pandas as pd
import pyodbc
import itertools
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
    "street1": "Your Address",
    "city": "Your City",
    "state": "Your State Abbreviation",
    "zip": "Your Zip",
    "country": "US"
}

# -----------------------------
# LOAD DATA
# -----------------------------

def load_items():
    df = pd.read_csv("item_dimensions.csv", engine="python")
    numeric = ["Weight","Length","Width","Height","UOM","ShipAloneQty"]
    for c in numeric:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    df["UOM"] = df["UOM"].replace(0,1)
    return df

def load_boxes():
    df = pd.read_csv("available_boxes.csv", engine="python")
    for c in ["Length","Width","Height"]:
        df[c] = pd.to_numeric(df[c])
    df["Volume"] = df["Length"] * df["Width"] * df["Height"]
    return df.sort_values("Volume")

items_df = load_items()
boxes_df = load_boxes()

# -----------------------------
# QUICKBOOKS
# -----------------------------

def qb_conn():
    return pyodbc.connect("DSN=QuickBooks Data;", autocommit=True)

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

    missing_items = merged[merged["Length"].isna()]["Item"].unique().tolist()
    merged = merged[merged["Length"].notna()]

    expanded = []

    for r in merged.itertuples():
        qty = int(r.Qty)
        weight = float(r.Weight)
        dims = (float(r.Length), float(r.Width), float(r.Height))
        uom = int(r.UOM) if not pd.isna(r.UOM) and r.UOM > 0 else 1
        ship_alone = int(r.ShipAloneQty) if not pd.isna(r.ShipAloneQty) else 0

        # ShipAlone items go in their own boxes
        if ship_alone > 0:
            full = qty // ship_alone
            remainder = qty % ship_alone
            for _ in range(full):
                expanded.append({
                    "item": r.Item,
                    "qty": ship_alone,
                    "weight": ship_alone*weight,
                    "dims": dims,
                    "alone": True
                })
            qty = remainder

        # UOM grouping
        if qty > 0:
            groups = qty // uom
            remainder = qty % uom
            for _ in range(groups):
                expanded.append({"item": r.Item, "qty": uom, "weight": uom*weight, "dims": dims, "alone": False})
            if remainder > 0:
                expanded.append({"item": r.Item, "qty": remainder, "weight": remainder*weight, "dims": dims, "alone": False})

    expanded.sort(key=lambda x: x["weight"], reverse=True)

    parcels = []

    for item in expanded:
        if item["alone"]:
            parcels.append({"items":[item], "weight":item["weight"], "dims":[item["dims"]]})
            continue

        placed=False
        for p in parcels:
            if all(not it["alone"] for it in p["items"]) and p["weight"] + item["weight"] <= MAX_BOX_WEIGHT:
                p["items"].append(item)
                p["weight"] += item["weight"]
                p["dims"].append(item["dims"])
                placed=True
                break
        if not placed:
            parcels.append({"items":[item], "weight":item["weight"], "dims":[item["dims"]]})

    results=[]
    for p in parcels:
        # Calculate L, W, H for box
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
            "Box": box_found.BoxName,
            "BoxDims": (float(box_found.Length), float(box_found.Width), float(box_found.Height)),
            "Weight": float(round(p["weight"],2)),
            "Items": p["items"]
        })

    return results, missing_items

# -----------------------------
# SHIPPO
# -----------------------------

def create_shippo_parcels(parcels):
    shippo_parcels=[]
    for p in parcels:
        dims = p.get("BoxDims") or p["Items"][0]["dims"]
        L, W, H = map(float, dims)
        weight = float(p["Weight"])
        parcel = components.ParcelCreateRequest(
            length=str(L),
            width=str(W),
            height=str(H),
            distance_unit=components.DistanceUnitEnum.IN,
            weight=str(weight),
            mass_unit=components.WeightUnitEnum.LB
        )
        shippo_parcels.append(parcel)
    return shippo_parcels

def get_fedex_rates(address, parcels):
    address_from = components.AddressCreateRequest(
        name="Warehouse",
        street1=str(SHIP_FROM["street1"]),
        city=str(SHIP_FROM["city"]),
        state=str(SHIP_FROM["state"]),
        zip=str(SHIP_FROM["zip"]),
        country="US"
    )

    address_to = components.AddressCreateRequest(
        name="Customer",
        street1=str(address["street1"]),
        city=str(address["city"]),
        state=str(address["state"]),
        zip=str(address["zip"]),
        country="US"
    )

    shipment_request = components.ShipmentCreateRequest(
        address_from=address_from,
        address_to=address_to,
        parcels=parcels
    )

    shipment = shippo_client.shipments.create(shipment_request)
    fedex_rates = [r for r in shipment.rates if r.provider == "FedEx"]
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

if not quote:
    st.error("Please enter a quote number")
    st.stop()

with st.spinner("Querying QuickBooks..."):
    header = get_estimate_header(quote)

if header.empty:
    st.error("Quote not found in QuickBooks")
    st.stop()

txn = header["TxnID"].iloc[0]

with st.spinner("Loading line items..."):
    lines = get_estimate_lines(txn)

with st.spinner("Calculating optimal boxes..."):
    parcels, missing_items = pack_items(lines)

st.subheader("📍 Ship To")
st.dataframe(header[["Street","City","State","Zip"]], width='stretch')

if missing_items:
    st.warning("⚠ Items missing from dimensions table")
    st.dataframe(pd.DataFrame({"Missing Items": missing_items}), width='stretch')

st.subheader("📦 Boxes")
shippo_parcels=[]

for i,p in enumerate(parcels,1):
    with st.container():
        st.markdown(f"### Box {i}")
        col1,col2 = st.columns(2)
        with col1:
            st.write("**Box Type:**", p["Box"])
            st.write("**Weight:**", p["Weight"], "lbs")
        with col2:
            item_counts={}
            for it in p["Items"]:
                item_counts[it["item"]] = item_counts.get(it["item"],0) + it["qty"]
            st.write("**Contents**")
            for item,qty in item_counts.items():
                st.write(f"- {item} x {qty}")

        shippo_parcels.append(create_shippo_parcels([p])[0])

# -----------------------------
# GET FEDEX RATES
# -----------------------------

if shippo_parcels:
    st.subheader("🚚 FedEx Shipping Rates")
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
        # Sort FedEx rates from cheapest to most expensive
        rates_sorted = sorted(rates, key=lambda r: float(r.amount))

        for r in rates_sorted:
            service = r.servicelevel.name
            cost = r.amount
            days = r.estimated_days if r.estimated_days else "N/A"
            st.write(f"**{service}** — ${cost} — {days} day(s)")
    else:
        st.warning("No FedEx rates returned")

