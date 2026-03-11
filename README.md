# QBDB-quotes-packager
Pull Quotes from QuickBooks using QODBC to determine what box(es) items are going into.

item_dimensions is where Item dimensions go obviosuly, but it will also need Unit of Measure for each item, if it differs from QuickBooks.
Our QuickBooks is set to every item is 1 so we need to know if a pack of 100 can be broken into different boxes or not.
We also need to denote if an item ships by itself and how many of that item will be going in it's own box.

available boxes just tells us what box sizes are available to pack into.
