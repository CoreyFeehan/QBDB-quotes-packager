import streamlit as st
import pandas as pd
import pyodbc
import warnings
import math
from shippo import Shippo
from shippo.models import components
from config import SHIPPO_API_KEY
from collections import defaultdict

warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy")

# -----------------------------
# CONFIG
# -----------------------------
MAX_BOX_WEIGHT = 35
shippo_client = Shippo(api_key_header=SHIPPO_API_KEY)
SHIP_FROM = {
    "name": "Warehouse",
    "street1": "Your Street",
    "city": "Your City",
    "state": "Your State",
    "zip": "Your Zip",
    "country": "US"
}

# -----------------------------
# LOAD DATA
# -----------------------------
def load_items():
    df = pd.read_csv("item_dimensions.csv", engine="python")
    numeric = ["Weight", "Length", "Width", "Height", "UOM", "ShipAloneQty"]
    for c in numeric:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    df["UOM"] = df["UOM"].replace(0, 1)
    return df

def load_boxes():
    df = pd.read_csv("available_boxes.csv", engine="python")
    df["Length"] = pd.to_numeric(df["Length"])
    df["Width"] = pd.to_numeric(df["Width"])
    df["Height"] = pd.to_numeric(df["Height"])
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
    conn = None
    try:
        conn = qb_conn()
        query = f"""
        SELECT
            TxnID,
            ShipAddressAddr1 AS Street,
            ShipAddressCity AS City,
            ShipAddressState AS State,
            ShipAddressPostalCode AS Zip,
            ShipAddressCountry AS Country
        FROM Estimate
        WHERE RefNumber = '{quote}'
        """
        return pd.read_sql(query, conn)
    finally:
        if conn:
            conn.close()

def get_estimate_lines(txn):
    conn = None
    try:
        conn = qb_conn()
        query = f"""
        SELECT
            EstimateLineItemRefFullName AS Item,
            EstimateLineQuantity AS Qty
        FROM EstimateLine
        WHERE TxnID = '{txn}'
        """
        return pd.read_sql(query, conn)
    finally:
        if conn:
            conn.close()

# -----------------------------
# PACK ITEMS (3D Packing)
# -----------------------------

def pack_items(lines):

    global items_df
    global boxes_df

    # -------------------------
    # Ignore QuickBooks shipping items
    # -------------------------
    lines = lines[~lines["Item"].str.lower().str.contains("shipping|freight|delivery", na=False)]
    lines = lines[lines["Item"].str.strip().astype(bool)]
    merged = lines.merge(items_df, on="Item", how="left")

    missing_items = merged[merged["Length"].isna()]["Item"].tolist()
    merged = merged[~merged["Length"].isna()].copy()

    # -------------------------
    # expand items
    # -------------------------
    expanded = []

    for _, r in merged.iterrows():

        qty = int(r["Qty"])
        uom = int(r.get("UOM",1))
        packs = math.ceil(qty / uom)

        for _ in range(packs):

            expanded.append({
                "item": r["Item"],
                "dims": (
                    float(r["Length"]),
                    float(r["Width"]),
                    float(r["Height"])
                ),
                "weight": float(r["Weight"]) * uom
            })

    # sort largest first
    expanded.sort(
        key=lambda x: x["dims"][0]*x["dims"][1]*x["dims"][2],
        reverse=True
    )

    boxes = boxes_df.sort_values("Volume").to_dict("records")

    parcels = []

    # -------------------------
    # rotations
    # -------------------------
    def rotations(d):

        l,w,h = d

        return [
            (l,w,h),(l,h,w),
            (w,l,h),(w,h,l),
            (h,l,w),(h,w,l)
        ]

    # -------------------------
    # grid capacity
    # -------------------------
    def capacity(box_dims,item_dims):

        bl,bw,bh = box_dims
        il,iw,ih = item_dims

        nx = int(bl // il)
        ny = int(bw // iw)
        nz = int(bh // ih)

        return nx*ny*nz

    remaining = expanded.copy()

    while remaining:

        best_box = None
        best_items = []
        best_score = 0

        for box in boxes:

            box_dims = (
                float(box["Length"]),
                float(box["Width"]),
                float(box["Height"])
            )

            max_weight = float(box.get("MaxWeight",MAX_BOX_WEIGHT))

            placed = []
            weight = 0

            for item in remaining:

                if weight + item["weight"] > max_weight:
                    continue

                placed_flag = False

                for r in rotations(item["dims"]):

                    cap = capacity(box_dims,r)

                    if cap <= 0:
                        continue

                    if len(placed) < cap:

                        placed.append({
                            "item": item["item"],
                            "dims": r,
                            "weight": item["weight"]
                        })

                        weight += item["weight"]
                        placed_flag = True
                        break

                if placed_flag and len(placed) >= cap:
                    break

            if not placed:
                continue

            weight_ratio = weight / max_weight
            score = len(placed) + weight_ratio

            if score > best_score:

                best_score = score
                best_box = box
                best_items = placed

        # -------------------------
        # item doesn't fit any box
        # -------------------------
        if not best_box:

            item = remaining.pop(0)

            l,w,h = item["dims"]

            parcels.append({
                "Box": f"{l}x{w}x{h}",
                "BoxDims": (l,w,h),
                "Weight": item["weight"],
                "Items":[item]
            })

            continue

        # remove packed items
        for placed in best_items:

            for i,r in enumerate(remaining):

                if r["item"] == placed["item"] and r["weight"] == placed["weight"]:
                    remaining.pop(i)
                    break

        parcels.append({
            "Box": best_box["BoxName"],
            "BoxDims": (
                float(best_box["Length"]),
                float(best_box["Width"]),
                float(best_box["Height"])
            ),
            "Weight": sum(i["weight"] for i in best_items),
            "Items": best_items
        })

    return parcels, missing_items

from collections import defaultdict

def group_identical_boxes(parcels):
    grouped = defaultdict(list)

    for p in parcels:
        # build a key based on sorted items, e.g. item name + qty
        counts = {}
        for it in p["Items"]:
            counts[it["item"]] = counts.get(it["item"], 0) + 1

        # sort so order doesn't matter and convert to tuple
        key = tuple(sorted(counts.items()))

        grouped[key].append(p)

    return grouped


# -----------------------------
# SHIPPO RATES
# -----------------------------
def create_shippo_parcels(parcels):
    shippo_parcels = []
    for p in parcels:
        dims = p["BoxDims"]
        parcel = components.ParcelCreateRequest(
            length=str(float(dims[0])),
            width=str(float(dims[1])),
            height=str(float(dims[2])),
            distance_unit=components.DistanceUnitEnum.IN,
            weight=str(float(p["Weight"])),
            mass_unit=components.WeightUnitEnum.LB
        )
        shippo_parcels.append(parcel)
    return shippo_parcels

def get_rates(address, parcels):
    address_from = components.AddressCreateRequest(**SHIP_FROM)
    address_to = components.AddressCreateRequest(
        name="Customer",
        street1=address["street1"],
        city=address["city"],
        state=address["state"],
        zip=address["zip"],
        country=address.get("country","US")
    )
    shipment_req = components.ShipmentCreateRequest(
        address_from=address_from,
        address_to=address_to,
        parcels=parcels
    )
    shipment = shippo_client.shipments.create(shipment_req)
    fedex_rates = [r for r in shipment.rates if r.provider=="FedEx"]
    if fedex_rates:
        fedex_rates.sort(key=lambda r: float(r.amount))
        return fedex_rates
    other = sorted(shipment.rates, key=lambda r: float(r.amount))
    return other

# -----------------------------
# STREAMLIT UI
# -----------------------------
st.title("ඞ Quote Shipping Planner ඞ")

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
    st.error("Quote not found")
    st.stop()

txn = header["TxnID"].iloc[0]
with st.spinner("Loading line items..."):
    lines = get_estimate_lines(txn)

with st.spinner("Calculating boxes with true 3D packing..."):
    parcels, missing_items = pack_items(lines)

st.subheader("📍 Ship To")
header["Country"] = header["Country"].fillna("US")
st.dataframe(header[["Street","City","State","Zip","Country"]], width='stretch', hide_index=True)

if missing_items:
    st.warning("⚠ Items missing from dimensions")
    st.dataframe(pd.DataFrame({"Missing Items": missing_items}), width='stretch')

st.subheader("📦 Boxes")
shippo_parcels=[]

for i,p in enumerate(parcels,1):
    shippo_parcels.append({
        "length": p["BoxDims"][0],
        "width": p["BoxDims"][1],
        "height": p["BoxDims"][2],
        "distance_unit":"in",
        "weight": p["Weight"],
        "mass_unit":"lb"
    })

groups = group_identical_boxes(parcels)

for key, box_list in groups.items():

    count = len(box_list)
    representative = box_list[0]

    # show how many identical boxes
    st.markdown(f"### 📦 {representative['Box']} — {count} {'boxes' if count > 1 else 'box'}")

    st.write(f"Dimensions: {representative['BoxDims']}")
    st.write(f"Weight: {int(representative['Weight'])} lbs")
    
    st.write("Contents:")
    for item_name, qty in key:
        st.write(f"- {item_name} x {qty}")

if shippo_parcels:
    st.subheader("🚚 Shipping Rates")
    address = {
        "street1": header["Street"].iloc[0],
        "city": header["City"].iloc[0],
        "state": header["State"].iloc[0],
        "zip": header["Zip"].iloc[0],
        "country": header["Country"].iloc[0]
    }
    with st.spinner("Getting Shipping Rates..."):
        rates = get_rates(address, shippo_parcels)
    if rates:
        for r in rates:
            service = r.servicelevel.name
            cost = r.amount
            days = r.estimated_days or "N/A"
            st.write(f"**{service}** — ${cost} — {days} day(s)")
    else:
        st.warning("No shipping rates returned")
