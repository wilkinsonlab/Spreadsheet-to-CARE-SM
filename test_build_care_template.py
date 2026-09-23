"""Regression tests for build_care_template.py.

Run with: python3 -m pytest test_build_care_template.py
"""
import build_care_template as bct
import profile_columns as pc


def test_build_resolvers_offline_uses_stubs():
    gene, myvariant = bct.build_resolvers(offline=True)
    assert isinstance(gene, pc.StubGeneResolver)
    assert isinstance(myvariant, pc.StubMyVariantResolver)


def test_build_resolvers_live_uses_real_resolvers():
    """Bug (2026-09-23): main() called pc.classify() with no gene_resolver/
    myvariant_resolver argument at all, so classify() silently fell through
    to ITS OWN defaults -- StubGeneResolver()/StubMyVariantResolver() --
    regardless of whether --offline was passed. A live run's GENE/VARIANT
    lanes were resolved against the tiny hardcoded stub lexicons instead of
    mygene.info/myvariant.info, with no error or warning: --offline and a
    live run behaved identically by accident. Fixed by extracting the
    offline/live choice into build_resolvers() and always passing its
    result into classify() explicitly (mirrors profile_columns.py's own
    main(), which never had this bug)."""
    gene, myvariant = bct.build_resolvers(offline=False)
    assert isinstance(gene, pc.GeneResolver)
    assert isinstance(myvariant, pc.MyVariantResolver)
    assert not isinstance(gene, pc.StubGeneResolver)
    assert not isinstance(myvariant, pc.StubMyVariantResolver)
