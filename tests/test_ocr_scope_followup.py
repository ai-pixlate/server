import copy
import pytest
from tests.test_ocr_scope import fixture
from tools.baseline.ocr_scope import filter_section, v5_inputs


def apply(s, g, l, out):
    before = copy.deepcopy((s, g, l, out))
    r = filter_section(s, g, out, image_sha256='img', gt_sha256='gt', gt_links=l)
    assert (s, g, l, out) == before
    return r


def test_empty_boundary_does_not_remove_target_gt():
    s, g, l = fixture()
    r = apply(s, g, l, [dict(id='empty', text='', rect=[0, 0, 100, 100])])
    assert r['ready'] and v5_inputs(r) == (['ABC'], [''])


def test_empty_does_not_bypass_unverified_gt_or_coverage():
    s, g, l = fixture()
    l['lines']['g1']['verified'] = False
    r = apply(s, g, l, [dict(id='empty', text='', rect=None)])
    assert not r['ready']


@pytest.mark.parametrize('text', [' ', '3', 'Good all goodal'])
def test_nonempty_crossing_not_silently_removed(text):
    s, g, l = fixture()
    r = apply(s, g, l, [dict(id='cross', text=text, rect=[0, 0, 100, 100])])
    assert not r['ready'] and r['audit'][-1]['decision'] == 'hold'


def test_policy_exclusion_requires_scope_agreement():
    s, g, l = fixture()
    l['lines']['g1'].update(required_decision='exclude', policy_evidence='user-note:hash')
    r = apply(s, g, l, [])
    assert not r['ready'] and r['audit'][0]['reason'] == 'policy_scope_connection_unresolved'


def test_exclusion_symmetric_and_caption_retained():
    s, g, l = fixture()
    s['regions'].append(dict(id='decoration', kind='exclude', rect=[0, 50, 100, 90], verified=True))
    l['lines']['g1'].update(rect=[10, 60, 40, 80], required_decision='exclude', policy_evidence='user-note:hash')
    out=[dict(id='decoration',text='ABC',rect=[10,60,40,80]),dict(id='caption',text='EXTRA',rect=[20,20,40,30])]
    r = apply(s, g, l, out)
    assert r['ready'] and v5_inputs(r) == ([], ['EXTRA'])
    assert r['audit'][0]['decision'] == r['audit'][1]['decision'] == 'exclude'


def test_layer_block_survives_all_records_geometrically_classified():
    s, g, l = fixture()
    s['unresolved_connections'] = ['background_foreground_overlap']
    r = apply(s, g, l, [])
    assert not r['ready']
    with pytest.raises(ValueError, match='section_scoring_blocked'):
        v5_inputs(r)
