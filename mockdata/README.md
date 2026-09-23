# mockdata/

Put spreadsheet exports here to try the tools against — real or synthetic.

**Nothing in this folder is ever committed** (see `.gitignore`): by project
convention, every sample-data file uses the `.mock` extension, and both
`*.mock` and this whole directory are git-ignored. This is a hard rule, not
just a default — patient data, real or realistically-shaped, must never end
up in version control.

See [`../docs/SYNTHETIC-TEST-DATA-COOKBOOK.md`](../docs/SYNTHETIC-TEST-DATA-COOKBOOK.md)
for how the project's own synthetic test files were built, if you want to
construct a similar adversarial fixture rather than use real data.
