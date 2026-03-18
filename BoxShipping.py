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
    "country": "Your Country"
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

def rotations(d):
    """Return all 6 axis-aligned rotations of a (l, w, h) tuple."""
    l, w, h = d
    return [(l,w,h),(l,h,w),(w,l,h),(w,h,l),(h,l,w),(h,w,l)]

def fits(space, dims):
    """Return True if dims fit inside the free space."""
    return dims[0] <= space[3] and dims[1] <= space[4] and dims[2] <= space[5]

def split_space(space, dims):
    x, y, z, sl, sw, sh = space
    l, w, h = dims
    spaces = []
    if sl - l > 0:
        spaces.append((x + l, y, z, sl - l, sw, sh))
    if sw - w > 0:
        spaces.append((x, y + w, z, l, sw - w, sh))
    if sh - h > 0:
        spaces.append((x, y, z + h, l, w, sh - h))
    return spaces

def remove_placed(remaining, best_items):
    for placed in best_items:
        for i in range(len(remaining) - 1, -1, -1):
            r = remaining[i]
            if r["item"] == placed["item"] and r["weight"] == placed["Weight"]:
                remaining.pop(i)
                break  # only remove one match per placed item
    return remaining

def pack_items(lines):
    global items_df, boxes_df

    lines = lines.copy()
    lines = lines[lines["Item"].notna()]
    lines = lines[~lines["Item"].str.lower().str.contains(
        "shipping|training", na=False)]

    merged = lines.merge(items_df, on="Item", how="left")

    missing_items = merged[merged["Length"].isna()]["Item"].tolist()
    merged = merged.groupby(["Item", "Weight", "Length", "Width", "Height", "UOM", "ShipAloneQty"],as_index=False)["Qty"].sum()

    duplicated_items = merged[merged.duplicated(subset=["Item"], keep=False)]["Item"].unique()

    expanded = []
    for _, r in merged.iterrows():
        qty = int(r["Qty"])
        uom = int(r.get("UOM", 1))
        packs = math.ceil(qty / uom)
        total_qty_per_pack = uom

        ship_alone = (
            bool(r.get("ShipAloneQty", 0) and qty >= r["ShipAloneQty"])
            or r["Item"] in duplicated_items
        )

        for _ in range(packs):
            expanded.append({
                "item": r["Item"],
                "dims": (float(r["Length"]), float(r["Width"]), float(r["Height"])),
                "weight": float(r["Weight"]) * uom,
                "Qty": total_qty_per_pack,
                "ship_alone": ship_alone
            })

    expanded.sort(key=lambda x: x["dims"][0] * x["dims"][1] * x["dims"][2], reverse=True)

    boxes = boxes_df.sort_values("Volume").to_dict("records")
    parcels = []
    remaining = expanded.copy()

    while remaining:

        ship_alone_idx = next(
            (i for i, item in enumerate(remaining) if item.get("ship_alone")), None
        )
        if ship_alone_idx is not None:
            item = remaining.pop(ship_alone_idx)
            parcels.append({
                "Box": f"{item['dims'][0]}x{item['dims'][1]}x{item['dims'][2]}",
                "BoxDims": item["dims"],
                "Weight": item["weight"],
                "Items": [{
                    "item": item["item"],
                    "Dims": item["dims"],
                    "Weight": item["weight"],
                    "Qty": item["Qty"]
                }]
            })
            continue

        best_box = None
        best_items = []
        best_count = 0
        best_weight = 0
        best_placed_indices = set()

        for box in boxes:
            box_dims = (
                float(box["Length"]),
                float(box["Width"]),
                float(box["Height"])
            )
            max_weight = float(box.get("MaxWeight", MAX_BOX_WEIGHT))
            spaces = [(0, 0, 0, box_dims[0], box_dims[1], box_dims[2])]
            placed = []
            placed_indices = set()
            weight = 0

            for idx, item in enumerate(remaining):
                if idx in placed_indices:
                    continue
                if weight + item["weight"] > max_weight:
                    continue

                spaces.sort(key=lambda s: s[3] * s[4] * s[5], reverse=True)

                placed_flag = False
                for space_i, space in enumerate(spaces):
                    for rot in rotations(item["dims"]):
                        if fits(space, rot):
                            placed.append({
                                "item": item["item"],
                                "Dims": rot,
                                "Weight": item["weight"],
                                "Qty": item["Qty"]
                            })
                            placed_indices.add(idx)
                            weight += item["weight"]
                            new_spaces = split_space(space, rot)
                            spaces.pop(space_i)
                            spaces.extend(new_spaces)
                            placed_flag = True
                            break
                    if placed_flag:
                        break

            if len(placed) > best_count:
                best_box = box
                best_items = placed
                best_count = len(placed)
                best_weight = weight
                best_placed_indices = placed_indices

        if not best_box:
            item = remaining.pop(0)
            parcels.append({
                "Box": f"{item['dims'][0]}x{item['dims'][1]}x{item['dims'][2]}",
                "BoxDims": item["dims"],
                "Weight": item["weight"],
                "Items": [{
                    "item": item["item"],
                    "Dims": item["dims"],
                    "Weight": item["weight"],
                    "Qty": item["Qty"]
                }]
            })
            continue

        # Remove by index in reverse — clean, no name/weight matching needed
        for idx in sorted(best_placed_indices, reverse=True):
            remaining.pop(idx)

        parcels.append({
            "Box": best_box["BoxName"],
            "BoxDims": (
                float(best_box["Length"]),
                float(best_box["Width"]),
                float(best_box["Height"])
            ),
            "Weight": best_weight,
            "Items": best_items
        })

    return parcels, missing_items


# -----------------------------
# Group identical boxes
# -----------------------------
def group_identical_boxes(parcels):
    grouped = defaultdict(list)
    for p in parcels:
        counts = {}
        for it in p["Items"]:
            counts[it["item"]] = counts.get(it["item"], 0) + it["Qty"]
        items_key = tuple(sorted(counts.items()))
        dims_key = tuple(p["BoxDims"])
        key = (items_key, dims_key)
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

MILITARY_STATES = {"AE", "AA", "AP"}



def get_rates(address, parcels):
    address = (address)
    state = (address.get("state") or "").strip().upper()
    is_military = state in MILITARY_STATES

    address_from = components.AddressCreateRequest(**SHIP_FROM)
    address_to = components.AddressCreateRequest(
        name="Customer",
        street1=address["street1"],
        city=address["city"],
        state=address["state"],
        zip=address["zip"],
        country=address.get("country", "US")
    )
    shipment_req = components.ShipmentCreateRequest(
        address_from=address_from,
        address_to=address_to,
        parcels=parcels
    )
    shipment = shippo_client.shipments.create(shipment_req)

    if is_military:
        # FedEx does not serve APO/FPO/DPO — filter to USPS only
        usps_rates = [r for r in shipment.rates if r.provider == "USPS"]
        if usps_rates:
            usps_rates.sort(key=lambda r: float(r.amount))
            return usps_rates
        return []
    else:
        fedex_rates = [r for r in shipment.rates if r.provider == "FedEx"]
        if fedex_rates:
            fedex_rates.sort(key=lambda r: float(r.amount))
            return fedex_rates
        return sorted(shipment.rates, key=lambda r: float(r.amount))

# -----------------------------
# STREAMLIT UI
# -----------------------------
st.title("Shipping Quotes")

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

with st.spinner("Calculating boxes..."):
    parcels, missing_items = pack_items(lines)

st.subheader("📍 Ship To")
header["Country"] = header["Country"].fillna("US")
st.dataframe(header[["Street","City","State","Zip","Country"]], width='stretch', hide_index=True)

if missing_items:
    st.warning("⚠ Items missing from dimensions")
    st.dataframe(pd.DataFrame({"Missing Items": missing_items}), width='stretch')

st.subheader("📦 Boxes")
shippo_parcels = []
groups = group_identical_boxes(parcels)

for (items_key, dims_key), box_list in groups.items():
    count = len(box_list)
    representative = box_list[0]

    st.markdown(f"### 📦 {representative['Box']} — {count} {'boxes' if count > 1 else 'box'}")
    st.write(f"Weight: {int(representative['Weight'])} lbs")

    st.write("Contents:")
    for item_name, qty in items_key:
        st.write(f"- {item_name} x {qty}")

    for _ in range(count):
        shippo_parcels.append({
            "length": dims_key[0],
            "width": dims_key[1],
            "height": dims_key[2],
            "distance_unit": "in",
            "weight": representative["Weight"],
            "mass_unit": "lb"
        })

if shippo_parcels:
    state = header["State"].iloc[0].strip().upper() if header["State"].iloc[0] else header["City"].iloc[0].strip().upper()
    is_military = state in {"AE", "AA", "AP"}

    st.subheader("🚚 Shipping Rates - Our Rate")
    if is_military:
        st.info("Military address not supported")

    address = {
        "street1": header["Street"].iloc[0],
        "city": header["City"].iloc[0] if header["City"].iloc[0]  else header["State"].iloc[0],
        "state": header["State"].iloc[0] if header["State"].iloc[0] else header["City"].iloc[0],
        "zip": header["Zip"].iloc[0] if header["Zip"].iloc[0] else "00000",
        "country": header["Country"].iloc[0]
    }
    with st.spinner("Getting Shipping Rates..."):
        rates = get_rates(address, shippo_parcels)
    if rates:
        for r in rates:
            service = r.servicelevel.name
            cost = r.amount
            days = r.estimated_days or "N/A"
            provider = r.provider
            st.write(f"**{service}** — ${cost} — {days} day(s) — {provider}")
    else:
        st.warning("No shipping rates returned")
