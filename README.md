# QBDB-quotes-packager
Pull Quotes from QuickBooks using QODBC to determine what box(es) items are going into, and return a shipping quote using our fedex account.

item_dimensions.csv is where item dimensions, weight, uom(if it differs than QuickBooks), and ShipAloneQty(tells us if that item has a quantity where it ships by itself with no other items, mostly for larger items).
Our QuickBooks is set to every item is 1, so a pack of 100 bags would be 100, and a pair of gloves would be 2.
So we need to know if 100 of an item can be broken into two boxes or if that 100 is a pack that can't be broken up.
We also need to denote if an item ships by itself and how many of that item will be going in it's own box.
Also the Length/Width/Height is for the full pack of the item whereas weight is always for one of that item.

available_boxes.csv just tells us what box sizes are available to pack into.
Optional, you can add a column, "MaxWeight", where you denote how much weight can be in that one box.

config.py contains your API key for shippo

on the main app(BoxShipping.py) there are few lines to edit that might matter.
MAX_BOX_WEIGHT tells us what max weight we are wanting per box, I set it to 35 lbs per box max. You can also set the max weight per box in available_boxes.csv
SHIP_FROM has your shipping address so shippo knows where we are shipping from.
in the function for pack_items there is an ignore list that can be edited to ignore items so they don't try to get thrown into a box, I remove shipping and other non item items like note is an item we have in QuickBooks that needs to be ignored.

QODBC - You'll need to make sure QuickBooks is open and running on the device this app runs on. And you'll have to be able to allow the app permissions in QuickBooks the first time it runs.

shippo - you will need to acquire a shippo developer API key, you can use the test key for testing purposes but it won't display fedex quotes until you get a developer key.
Also you will need to connect you business's shipping accounts on shippo's website to get your rates for shipping.
