import copy
import itertools
import random
import pytest
from tools.baseline.ocr import edits
from tools.baseline.ocr_v5 import score_lines, distance, assignment


@pytest.mark.parametrize('gt,out,errors', [
    (['APPLE','BANANA'],['BANANA','APPLE'],0),
    (['ABCD'],['ABXD'],1),
    (['ABCD'],['AB','XD'],1),
    (['AB','CD'],['ABXD'],1),
    (['ABCD'],['AB','CD'],0),
    (['AB','CD'],['ABCD'],0),
    (['AB','CD'],['AB'],2),
    (['AB'],['AB','AB'],2),
    (['AB','AB'],['AB'],2),
    (['AB'],['AB','XYZ'],3),
    (['AB'],['BA'],2),
    (['가 나'],['가\t나'],0),
    (['A'],[],1),
    ([],['ABC'],3),
    ([''],[],0),
    (['본문'],['본문','미전사'],3),
])
def test_cases(gt,out,errors):
    before=copy.deepcopy((gt,out))
    r=score_lines(gt,out)
    assert r['errors']==errors
    assert (gt,out)==before
    assert r==score_lines(gt,out)
    assert sorted(i for p in r['matches'] for i in p['gt_indices'])==[i for i,x in enumerate(gt) if x]


def test_bit_distance_independent():
    rng=random.Random(501)
    for _ in range(300):
        a=''.join(rng.choices('AB가·',k=rng.randrange(30)))
        b=''.join(rng.choices('AB가·',k=rng.randrange(30)))
        r=edits(a,b)
        assert distance(a,b)==r['S']+r['D']+r['I']


def test_assignment_bruteforce():
    rng=random.Random(502)
    for n in range(1,6):
        c=[[rng.randrange(10) for _ in range(n)] for _ in range(n)]
        assert sum(c[i][j] for i,j in assignment(c))==min(sum(c[i][j] for i,j in enumerate(p)) for p in itertools.permutations(range(n)))


def test_bad_input_and_empty_denominator():
    with pytest.raises(ValueError):
        score_lines(['A'],[None])
    assert score_lines([],['ABC'])['cer_percent'] is None


def test_arbitrary_identical_concatenation():
    assert score_lines(['ABCDEFGH'],['AB','CD','EF','GH'])['errors']==0
    assert score_lines(['ABCDEFGH'],['AB','CD','XF','GH'])['errors']==1


def test_candidate_never_adds_boundary_penalty_over_v4():
    rng=random.Random(503)
    for _ in range(50):
        g=[''.join(rng.choices('ABC',k=5)) for _ in range(4)]
        p=[''.join(rng.choices('ABC',k=4)) for _ in range(5)]
        assert score_lines(g,p)['errors']<=distance(''.join(g),''.join(p))
