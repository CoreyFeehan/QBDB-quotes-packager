import streamlit as st
import pandas as pd
import pyodbc
import warnings
import math
import requests
from config import FEDEX_CLIENT_ID, FEDEX_CLIENT_SECRET, FEDEX_ACCOUNT_NUMBER, SHIPPO_API_KEY
from collections import defaultdict

warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy")

# -----------------------------
# CONFIG
# -----------------------------
MAX_BOX_WEIGHT = 35
SHIP_FROM = {
    "name": "The White House",
    "street1": "1600 Pennsylvania Ave", #Change to your street
    "city": "Washington", #Change to your city
    "state": "DC", #Change to your state
    "zip": "20500", #Change to your Zip
    "country": "US",
    "phone": "5555555555", #Can be any phone number, USPS just needs a phone number
    "email": "email@gmail.com" #Can be any email, USPS just needs an email
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

def get_salesorder_header(so_number):
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
        FROM SalesOrder
        WHERE RefNumber = '{so_number}'
        """
        return pd.read_sql(query, conn)
    finally:
        if conn:
            conn.close()

def get_salesorder_lines(txn):
    conn = None
    try:
        conn = qb_conn()
        query = f"""
        SELECT
            SalesOrderLineItemRefFullName AS Item,
            SalesOrderLineQuantity AS Qty
        FROM SalesOrderLine
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
        "shipping|a-note|a-intl-terms|repair|a-fsis-training", na=False)]

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
# FEDEX RATES
# -----------------------------
MILITARY_STATES = {"AE", "AA", "AP"}

FEDEX_SERVICE_NAMES = {
    "FEDEX_GROUND":         "FedEx Ground",
    "GROUND_HOME_DELIVERY": "FedEx Home Delivery",
    "FEDEX_2_DAY":          "FedEx 2Day",
    "FEDEX_2_DAY_AM":       "FedEx 2Day AM",
    "FEDEX_EXPRESS_SAVER":  "FedEx Express Saver",
    "STANDARD_OVERNIGHT":   "FedEx Standard Overnight",
    "PRIORITY_OVERNIGHT":   "FedEx Priority Overnight",
    "FIRST_OVERNIGHT":      "FedEx First Overnight",
}

def get_fedex_token():
    resp = requests.post(
        "https://apis.fedex.com/oauth/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_id": FEDEX_CLIENT_ID,
            "client_secret": FEDEX_CLIENT_SECRET,
        }
    )
    if not resp.ok:
        raise Exception(f"FedEx auth failed ({resp.status_code}): {resp.text}")
    return resp.json()["access_token"]

def get_fedex_rates(address, parcels):
    token = get_fedex_token()

    packages = []
    for i, p in enumerate(parcels):
        packages.append({
            "sequenceNumber": i + 1,
            "weight": {"units": "LB", "value": round(float(p["weight"]), 1)},
            "dimensions": {
                "length": int(float(p["length"])),
                "width": int(float(p["width"])),
                "height": int(float(p["height"])),
                "units": "IN"
            }
        })

    payload = {
        "accountNumber": {"value": FEDEX_ACCOUNT_NUMBER},
        "rateRequestControlParameters": {
            "returnTransitTimes": True
        },
        "requestedShipment": {
            "shipper": {
                "address": {
                    "streetLines": [SHIP_FROM["street1"]],
                    "city": SHIP_FROM["city"],
                    "stateOrProvinceCode": SHIP_FROM["state"],
                    "postalCode": SHIP_FROM["zip"],
                    "countryCode": SHIP_FROM["country"]
                }
            },
            "recipient": {
                "address": {
                    "streetLines": [address["street1"]],
                    "city": address["city"],
                    "stateOrProvinceCode": address["state"],
                    "postalCode": address["zip"],
                    "countryCode": address.get("country", "US")
                }
            },
            "pickupType": "DROPOFF_AT_FEDEX_LOCATION",
            "rateRequestType": ["ACCOUNT", "LIST"],
            "requestedPackageLineItems": packages
        }
    }

    resp = requests.post(
        "https://apis.fedex.com/rate/v1/rates/quotes",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload
    )
    resp.raise_for_status()

    results = {}
    for detail in resp.json().get("output", {}).get("rateReplyDetails", []):
        service_type = detail.get("serviceType", "")
        transit = (
            detail.get("operationalDetail", {}).get("transitTime")
            or detail.get("commit", {}).get("transitDays", {}).get("minimumTransitTime")
            or "N/A"
        )
        entry = {"transit": transit}
        for shipment_detail in detail.get("ratedShipmentDetails", []):
            rate_type = shipment_detail.get("rateType")
            if rate_type == "ACCOUNT":
                entry["account"] = float(shipment_detail.get("totalNetCharge", 0))
            elif rate_type == "LIST":
                entry["list"] = float(shipment_detail.get("totalNetCharge", 0))
        if entry:
            results[service_type] = entry
    return results

def get_usps_rates(address, parcels):
    """Send one request per parcel — USPS does not support multi-parcel shipments via Shippo."""
    address_from = {
        "name": SHIP_FROM["name"],
        "street1": SHIP_FROM["street1"],
        "city": SHIP_FROM["city"],
        "state": SHIP_FROM["state"],
        "zip": SHIP_FROM["zip"],
        "country": SHIP_FROM["country"],
        "phone": SHIP_FROM["phone"],
        "email": SHIP_FROM["email"]
    }
    address_to = {
        "name": "Customer",
        "street1": address["street1"],
        "city": address["city"],
        "state": address["state"],
        "zip": address["zip"],
        "country": address.get("country", "US"),
    }
    customs = {
        "contents_type": "MERCHANDISE",
        "non_delivery_option": "RETURN",
        "certify": True,
        "certify_signer": "Warehouse",
        "eel_pfc": "NOEEI_30_37_a",
        "items": [
            {
                "description": "General merchandise",
                "quantity": 1,
                "net_weight": "1",
                "mass_unit": "lb",
                "value_amount": "100",
                "value_currency": "USD",
                "origin_country": "US",
            }
        ],
    }

    responses = []
    for p in parcels:
        payload = {
            "address_from": address_from,
            "address_to": address_to,
            "parcels": [{
                "length": str(round(float(p["length"]), 2)),
                "width": str(round(float(p["width"]), 2)),
                "height": str(round(float(p["height"]), 2)),
                "distance_unit": "in",
                "weight": str(round(float(p["weight"]), 2)),
                "mass_unit": "lb",
            }],
            "customs_declaration": customs,
            "async": False,
        }
        resp = requests.post(
            "https://api.goshippo.com/shipments/",
            headers={
                "Authorization": f"ShippoToken {SHIPPO_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        responses.append((resp.json(), payload))

    return responses

# -----------------------------
# STREAMLIT UI
# -----------------------------
st.title("Shipping Quotes")

with st.form("quote_form"):
    doc_type = st.radio("Transaction Type", ["Estimate", "Sales Order"], horizontal=True)
    label = "QuickBooks Transaction Number"
    quote = st.text_input(label, key="doc_number")
    submitted = st.form_submit_button("Calculate Boxes")

if not submitted:
    st.stop()
if not quote:
    st.error("Please enter a number")
    st.stop()

with st.spinner("Querying QuickBooks..."):
    if doc_type == "Estimate":
        header = get_estimate_header(quote)
    else:
        header = get_salesorder_header(quote)

if header.empty:
    st.error(f"{doc_type} not found")
    st.stop()

txn = header["TxnID"].iloc[0]
with st.spinner("Loading line items..."):
    if doc_type == "Estimate":
        lines = get_estimate_lines(txn)
    else:
        lines = get_salesorder_lines(txn)

with st.spinner("Calculating boxes..."):
    parcels, missing_items = pack_items(lines)

st.subheader("📍 Ship To")
header["Country"] = header["Country"].fillna("US")
st.dataframe(header[["Street","City","State","Zip","Country"]], width='stretch', hide_index=True)

if missing_items:
    st.warning("⚠ Items missing from dimensions")
    st.dataframe(pd.DataFrame({"Missing Items": missing_items}), width='stretch')

st.subheader("📦 Boxes")
api_parcels = []
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
        api_parcels.append({
            "length": dims_key[0],
            "width": dims_key[1],
            "height": dims_key[2],
            "weight": representative["Weight"],
        })

if api_parcels:
    state = header["State"].iloc[0].strip().upper() if header["State"].iloc[0] else header["City"].iloc[0].strip().upper()
    is_military = state in MILITARY_STATES

    st.subheader("🚚 Shipping Rates")

    address = {
        "street1": header["Street"].iloc[0],
        "city": header["City"].iloc[0] if header["City"].iloc[0] else header["State"].iloc[0],
        "state": header["State"].iloc[0] if header["State"].iloc[0] else header["City"].iloc[0],
        "zip": header["Zip"].iloc[0] if header["Zip"].iloc[0] else "00000",
        "country": header["Country"].iloc[0]
    }

    if is_military:
        st.info("Military address (APO/FPO/DPO) — showing USPS rates only. Insurance not included, insurance is entire cost of all items times .184 ")

        # USPS requires country=US and city=APO/FPO/DPO for military addresses
        military_address = {**address, "country": "US"}
        raw_city = (address.get("city") or "").strip().upper()
        if raw_city not in ("APO", "FPO", "DPO"):
            military_address["city"] = {"AE": "APO", "AA": "APO", "AP": "FPO"}.get(state, "APO")

        with st.spinner("Getting USPS Rates..."):
            try:
                parcel_responses = get_usps_rates(military_address, api_parcels)
            except Exception as e:
                parcel_responses = []
                st.warning(f"Could not retrieve USPS rates: {e}")

        # Aggregate rates by service: sum amounts across all parcels
        service_totals = {}
        for raw, _ in parcel_responses:
            for r in raw.get("rates", []):
                key = r.get("servicelevel", {}).get("token", "")
                if key not in service_totals:
                    service_totals[key] = {
                        "name": r.get("servicelevel", {}).get("name", "N/A"),
                        "amount": 0.0,
                        "estimated_days": r.get("estimated_days"),
                        "provider": r.get("provider", "USPS"),
                    }
                service_totals[key]["amount"] += float(r["amount"])

        usps_rates = list(service_totals.values())
        if usps_rates:
            usps_rates.sort(key=lambda r: r["amount"])
            rate_rows = [
                {
                    "Service": r["name"],
                    "Est. Days": r["estimated_days"] or "N/A",
                    "Rate": f"${r['amount']:.2f}",
                }
                for r in usps_rates
            ]
            st.dataframe(pd.DataFrame(rate_rows), hide_index=True, width='stretch')
        else:
            st.warning("No rates returned from Shippo")
    else:
        with st.spinner("Getting Shipping Rates..."):
            try:
                fedex_rates = get_fedex_rates(address, api_parcels)
            except Exception as e:
                fedex_rates = {}
                st.warning(f"Could not retrieve FedEx rates: {e}")

        if fedex_rates:
            rate_rows = []
            for service_type, data in sorted(fedex_rates.items(), key=lambda x: x[1].get("account", 9999)):
                row = {
                    "Service": FEDEX_SERVICE_NAMES.get(service_type, service_type),
                    "Est. Days": data.get("transit", "N/A"),
                    "Our Rate": f"${data['account']:.2f}" if "account" in data else "N/A",
                    "List Rate": f"${data['list']:.2f}" if "list" in data else "N/A",
                }
                rate_rows.append(row)
            st.dataframe(pd.DataFrame(rate_rows), hide_index=True, width='stretch')
        else:
            st.warning("No shipping rates returned")
