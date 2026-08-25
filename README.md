# Spreadsheet to CARE-SM

## Proposal

Preamble: Someone exports a spreadsheet of data from their registry.  Some columns represent ~directly usable data (e.g. dates/times), comments, etc.  Other columns represent the "meat" of the data (phenotype, diagnosis, blood results, etc.).  CARE-SM is a controlled vocabulary and data model for clinial observations.  It has about 20 classes representing the various types of observations/events, such as Phenotype, laboratory measurements, physical measurements, etc.

Speculation #1: It should be possible to automatically determine which columns can be ontologically mapped/transformed by simply taking a selection of values from that column and running them against the nmdo-search.  If they are getting "hits", then they can probably be mapped.  There are two scenarios for a column.  First, the column header could be ontologically mappable (e.g. column header is "10MWT", with the column values being numerical times).  The second possibility is that the column values are mappable (e.g. "has trouble swallowing") which would certainly map to a Human Phenotype Ontology term, which would tell us that this is a Phenotype column.

Speculation #2: The branch of the ontology that they are mapping to will tell us what "kind" of data they represent.  e.g. if they are mapping to HPO terms, then those observations are probably Phenotype, but if they are mapping to something else like  NCIT, they might represent blood chemistry results. In principle, each "ontology type" should represent a "data type" in the CARE-SM model.

Speculation #3: given #1 and #2, it should be possible to heuristially detect when a column belongs in a specific CARE-SM data model, extract only the columns necessary for that model, execute the transformations, and write-out a valid CARE-SM template.  Repeat for every mappable column.

## How it turned out

All three speculations survived testing, but none survived unmodified.

**See [PIPELINE.md](PIPELINE.md)** for the end-to-end design: workflow diagram, the
column-routing decision tree, the resources we assemble, stage-by-stage detail, and the
validation measurements.

**See [talk/](talk/)** for the presentation deck (`spreadsheet-to-care-deck.pptx`) and its
textual companion, [`talk/DECISION-WORKFLOW.md`](talk/DECISION-WORKFLOW.md) — every column-triage
decision, in order, with the exact rule and whether/how the ontology search is involved.

See [CHANGELOG.md](CHANGELOG.md) for what changed and when.

