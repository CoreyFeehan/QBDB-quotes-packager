import streamlit as st
import pandas as pd
import pyodbc
import warnings
import itertools
import math

warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy")

MAX_BOX_WEIGHT = 40


# -----------------------------
# LOAD DATA
# -----------------------------

def load_items():
    df = pd.read_csv("item_dimensions.csv")

    numeric = ["Weight","Length","Width","Height","UOM","ShipAloneQty"]

    for c in numeric:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    df["UOM"] = df["UOM"].replace(0,1)

    return df


def load_boxes():

    df = pd.read_csv("available_boxes.csv")

    df["Length"] = pd.to_numeric(df["Length"])
    df["Width"] = pd.to_numeric(df["Width"])
    df["Height"] = pd.to_numeric(df["Height"])

    df["Volume"] = df["Length"] * df["Width"] * df["Height"]

    df = df.sort_values("Volume")

    return df


# Load each run (no cache)
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
    conn = None
    try:
        conn = qb_conn()
        query = f"""
        SELECT
            TxnID,
            ShipAddressAddr1 AS Street,
            ShipAddressCity AS City,
            ShipAddressState AS State,
            ShipAddressPostalCode AS Zip
        FROM Estimate
        WHERE RefNumber = '{quote}'
        """
        df = pd.read_sql(query, conn)
        return df
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
        df = pd.read_sql(query, conn)
        return df
    finally:
        if conn:
            conn.close()


# -----------------------------
# FIND SMALLEST BOX
# -----------------------------

def rotations(l, w, h):
    """Return all unique rotations of a box."""
    return list(set(itertools.permutations([l, w, h], 3)))


def find_box(length, width, height):

    item_volume = length * width * height

    best_box = None
    best_waste = None
    best_rotation = None

    for rot in rotations(length, width, height):

        rl, rw, rh = rot

        for _, box in boxes_df.iterrows():

            if (
                rl <= box.Length
                and rw <= box.Width
                and rh <= box.Height
            ):

                box_volume = box.Volume
                waste = box_volume - item_volume

                if best_waste is None or waste < best_waste:

                    best_box = box
                    best_waste = waste
                    best_rotation = rot

    # If nothing fits, use the largest box
    if best_box is None:

        largest = boxes_df.iloc[-1]

        return (
            largest.BoxName,
            (largest.Length, largest.Width, largest.Height),
            (length, width, height)
        )

    return (
        best_box.BoxName,
        (best_box.Length, best_box.Width, best_box.Height),
        best_rotation
    )


# -----------------------------
# PACKING ALGORITHM
# -----------------------------

def pack_items(lines):
    # Merge with item dimensions
    merged = lines.merge(items_df, on="Item", how="left")

    # Ignore shipping or note rows
    ignore = ["shipping", "a-intlterms", "a-note"]
    merged = merged[~merged["Item"].str.lower().isin(ignore)]
    merged = merged[(merged["Qty"].notna()) & (merged["Qty"] > 0)]

    expanded = []

    # Expand quantities, handle ShipAloneQty
    for r in merged.itertuples():
        qty = int(r.Qty)
        weight = r.Weight
        dims = (r.Length, r.Width, r.Height)
        uom = int(r.UOM) if not pd.isna(r.UOM) and r.UOM > 0 else 1
        ship_alone = int(r.ShipAloneQty) if not pd.isna(r.ShipAloneQty) else 0

        if ship_alone > 0:
            # Each "ship alone" batch is a separate parcel
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

        # Handle remaining qty normally
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

    # Sort largest items first
    expanded.sort(key=lambda x: x["weight"], reverse=True)

    # Precompute best rotation per item (min footprint)
    for item in expanded:
        item["dims"] = min(rotations(*item["dims"]), key=lambda r: r[0]*r[1])

    parcels = []

    # Pack items
    for item in expanded:
        if item["alone"]:
            # Ship alone → always new parcel
            parcels.append({
                "items": [item],
                "weight": item["weight"],
                "dims": [item["dims"]]
            })
            continue

        # First Fit Decreasing for non-ship-alone items
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
                "items": [item],
                "weight": item["weight"],
                "dims": [item["dims"]]
            })

    # Assign boxes
    results = []
    for p in parcels:
        # Approximate parcel dimensions using a square footprint heuristic
        total_area = sum(l*w for l, w, h in p["dims"])
        max_length = max(l for l, w, h in p["dims"])
        max_width = max(w for l, w, h in p["dims"])
        max_height = max(h for l, w, h in p["dims"])

        # Estimate a roughly square layout for L and W
        side = math.ceil(math.sqrt(total_area))
        L = max(side, max_length)
        W = max(side, max_width)
        H = max_height

        # Ship-alone parcel uses item dims as box
        if len(p["items"]) == 1 and p["items"][0]["alone"]:
            item = p["items"][0]
            box_dims = item["dims"]
            box_name = f"{box_dims[0]} x {box_dims[1]} x {box_dims[2]}"
            rotation_used = [box_dims]
        else:
            # Use first box that fits
            box_found = None
            for _, box in boxes_df.iterrows():
                if L <= box.Length and W <= box.Width and H <= box.Height:
                    box_found = box
                    break
            if box_found is None:
                box_found = boxes_df.iloc[-1]

            box_name = box_found.BoxName
            box_dims = (box_found.Length, box_found.Width, box_found.Height)
            rotation_used = [item["dims"] for item in p["items"]]

        results.append({
            "Box": box_name,
            "BoxDims": box_dims,
            "RotationUsed": rotation_used,
            "Weight": round(p["weight"], 2),
            "Items": p["items"]
        })

    return results


# -----------------------------
# STREAMLIT UI
# -----------------------------

st.title("📦 Shipping Box Planner")

quote = st.text_input("QuickBooks Quote Number")

if st.button("Calculate Boxes"):

    if not quote:
        st.error("Enter a quote number")
        st.stop()

    header = get_estimate_header(quote)

    if header.empty:
        st.error("Quote not found")
        st.stop()

    txn = header["TxnID"].iloc[0]

    lines = get_estimate_lines(txn)

    parcels = pack_items(lines)

    st.subheader("Ship To")

    st.write(header[["Street","City","State","Zip"]])

    st.subheader("Boxes")

    for i, p in enumerate(parcels, 1):
        st.markdown(f"### Box {i}")
        st.write(f"Box Type: {p['Box']}")
        st.write(f"Weight: {p['Weight']} lbs")

        # Combine items with same name
        combined = {}
        for it in p["Items"]:
            name = it["item"]
            combined[name] = combined.get(name, 0) + it["qty"]

        for name, qty in combined.items():
            st.write(f"- {name} x {qty}")