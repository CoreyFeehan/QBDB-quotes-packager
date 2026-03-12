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

    df = pd.read_csv("item_dimensions.csv", engine="python")

    numeric = ["Weight","Length","Width","Height","UOM","ShipAloneQty"]

    for c in numeric:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    df["UOM"] = df["UOM"].replace(0,1)

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

    merged=lines.merge(items_df,on="Item",how="left")

    ignore=["shipping"]

    merged=merged[~merged["Item"].str.lower().isin(ignore)]
    merged=merged[(merged["Qty"].notna()) & (merged["Qty"]>0)]

    expanded=[]

    for r in merged.itertuples():

        qty=int(r.Qty)

        weight=r.Weight

        dims=(r.Length,r.Width,r.Height)

        uom=int(r.UOM) if not pd.isna(r.UOM) and r.UOM>0 else 1

        ship_alone=int(r.ShipAloneQty) if not pd.isna(r.ShipAloneQty) else 0


        if ship_alone>0:

            full=qty//ship_alone
            remainder=qty%ship_alone

            for _ in range(full):

                expanded.append({
                    "item":r.Item,
                    "qty":ship_alone,
                    "weight":ship_alone*weight,
                    "dims":dims,
                    "alone":True
                })

            qty=remainder


        if qty>0:

            groups=qty//uom
            remainder=qty%uom

            for _ in range(groups):

                expanded.append({
                    "item":r.Item,
                    "qty":uom,
                    "weight":uom*weight,
                    "dims":dims,
                    "alone":False
                })

            if remainder>0:

                expanded.append({
                    "item":r.Item,
                    "qty":remainder,
                    "weight":remainder*weight,
                    "dims":dims,
                    "alone":False
                })


    expanded.sort(key=lambda x:x["weight"],reverse=True)


    for item in expanded:

        item["dims"]=min(rotations(*item["dims"]),key=lambda r:r[0]*r[1])


    parcels=[]


    for item in expanded:

        if item["alone"]:

            parcels.append({
                "items":[item],
                "weight":item["weight"],
                "dims":[item["dims"]]
            })

            continue


        placed=False

        for p in parcels:

            if all(not it["alone"] for it in p["items"]) and p["weight"]+item["weight"]<=MAX_BOX_WEIGHT:

                p["items"].append(item)

                p["weight"]+=item["weight"]

                p["dims"].append(item["dims"])

                placed=True

                break


        if not placed:

            parcels.append({
                "items":[item],
                "weight":item["weight"],
                "dims":[item["dims"]]
            })


    results=[]


    for p in parcels:

        total_area=sum(l*w for l,w,h in p["dims"])

        max_length=max(l for l,w,h in p["dims"])
        max_width=max(w for l,w,h in p["dims"])
        max_height=max(h for l,w,h in p["dims"])

        side=math.ceil(math.sqrt(total_area))

        L=max(side,max_length)
        W=max(side,max_width)
        H=max_height


        if len(p["items"])==1 and p["items"][0]["alone"]:

            dims=p["items"][0]["dims"]

            box_name=f"{dims[0]} x {dims[1]} x {dims[2]}"

            box_dims=dims

        else:

            box_found=None

            for _,box in boxes_df.iterrows():

                if L<=box.Length and W<=box.Width and H<=box.Height:

                    box_found=box
                    break

            if box_found is None:
                box_found=boxes_df.iloc[-1]

            box_name=box_found.BoxName

            box_dims=(box_found.Length,box_found.Width,box_found.Height)


        results.append({
            "Box":box_name,
            "BoxDims":box_dims,
            "Weight":round(p["weight"],2),
            "Items":p["items"]
        })

    return results


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
        street1=address["Street"],
        city=address["City"],
        state=address["State"],
        zip=address["Zip"],
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

quote=st.text_input("QuickBooks Quote Number")

if st.button("Calculate Shipping"):

    if not quote:
        st.error("Enter quote number")
        st.stop()


    header=get_estimate_header(quote)

    if header.empty:
        st.error("Quote not found")
        st.stop()


    txn=header["TxnID"].iloc[0]

    lines=get_estimate_lines(txn)


    parcels=pack_items(lines)

    st.subheader("Ship To")

    st.write(header[["Street","City","State","Zip"]])


    st.subheader("Boxes")

    for i,p in enumerate(parcels,1):

        st.markdown(f"### Box {i}")

        st.write(f"Box Type: {p['Box']}")
        st.write(f"Weight: {p['Weight']} lbs")

        combined={}

        for it in p["Items"]:
            combined[it["item"]]=combined.get(it["item"],0)+it["qty"]

        for name,qty in combined.items():
            st.write(f"- {name} x {qty}")


    with st.spinner("Getting FedEx Rates..."):

        shippo_parcels=create_shippo_parcels(parcels)

        address=header.iloc[0]

        rates=get_fedex_rates(address,shippo_parcels)


    st.subheader("FedEx Rates")

    for r in rates:

        st.write(
            f"{r.servicelevel.name} — ${r.amount} ({r.estimated_days} days)"
        )


