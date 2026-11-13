from pathlib import Path
import json,sys,collections,hashlib
from PIL import Image
ROOT=Path('C:/Users/Yeram/Documents/pixlate-server');W=ROOT/'.worktrees/ai-pipeline-evaluation-metrics';sys.path.insert(0,str(W))
from tools.baseline.core import read,digest,write_new
from tools.baseline.ocr_prepare import candidates
run=W/'docs/ai-baseline/runs/baseline-v1-0-1-gs-v1-20261030-01'
legacy=ROOT/'pipeline/out/ocr_input/v1';older=ROOT/'pipeline/samples/local/merge-input-v1'
origroot=ROOT/'pipeline/samples/evaluation/gs-v1'
dataset=read(W/'pipeline/samples/evaluation/gs-v1/manifest.json')
truth=[json.loads(l) for l in (W/'docs/ai-baseline/references/ocr-text/ocr-text-v1-20261104/text_units.jsonl').read_text(encoding='utf-8').splitlines()]
truthby={(u['sample'],u['image']['sha256']):u for u in truth}
events=candidates([json.loads(l) for l in (run/'stages.jsonl').read_text(encoding='utf-8').splitlines()])
objects=read(run/'object-index.json');db={c:read(run/f'evidence/{c}.json') for c in ['GS-01','GS-02','GS-03']}
combined=read(W/'docs/ai-baseline/evaluations/ocr-v4-v1-0-1-20261105/combined-inputs.json')['sections']
rows=[];sources=[];errors=[]
for orig in dataset['files']:
 sid=orig['source_id'];case=orig['product_id'];folder=legacy/sid;sp=read(folder/'split.json');prov=read(folder/'provenance.json')
 op=origroot/orig['path'];assert digest(op)==orig['sha256'];im=Image.open(op).convert('RGB')
 previous=0
 for sec in sp['sections']:
  f=folder/sec['image_path'];actual=digest(f);size=Image.open(f).size
  expected=next(x['sha256'] for x in prov['sections'] if x['section_key']==sec['section_key'])
  contiguous=sec['top_offset']==previous;previous=sec['top_offset']+sec['height']
  with Image.open(f) as si:
   crop_equal=si.convert('RGB').tobytes()==im.crop((0,sec['top_offset'],sec['width'],previous)).tobytes()
  if not (actual==expected and size==(sec['width'],sec['height']) and contiguous and crop_equal):errors.append([sid,sec['section_key']])
  truthunit=truthby.get((sid,actual));matches=[]
  for e in events:
   if e['case']!=case:continue
   source=next((s for s in db[case]['source_image'] if s['id']==e['source_image_id']),None)
   if not source or Path(source['file_url']).stem!=sid:continue
   es=e['section'];shape=(sec['top_offset'],sec['width'],sec['height'])
   if (es['top_offset'],es['width'],es['height'])!=shape:continue
   saved=[s for s in db[case]['section'] if s['source_image_id']==e['source_image_id'] and (s['top_offset'],s['bbox']['w'],s['height'])==shape]
   keys=[s['image_key'] for s in saved if objects.get(s.get('image_key'),{}).get('sha256')==actual]
   for key in keys:assert digest(run/'objects'/actual)==actual
   matches.append({'line':e['line'],'section_key':es['section_key'],'image_hash_proven':bool(keys),'seconds':e['seconds']})
  baseline_reuse=len(matches)==1 and matches[0]['image_hash_proven']
  output=older/sid/'ocr'/f"{sec['section_key']}.json"
  olddata=read(output) if output.exists() else None
  oldrun=older/sid/'ocr_run'/f"{sec['section_key']}.run.json"
  oldmeta=read(oldrun) if oldrun.exists() else None
  oldimg=older/sid/'sections'/f"{sec['section_key']}.png"
  oldmatch=oldimg.exists() and digest(oldimg)==actual
  supplemental=bool(truthunit and next(r for r in combined if r['id']==truthunit['id'])['origin']=='supplement_20261105')
  rows.append({'source_id':sid,'section_key':sec['section_key'],'path':f.as_posix(),'sha256':actual,'bytes':f.stat().st_size,
   'width':size[0],'height':size[1],'top_offset':sec['top_offset'],'provenance_hash_match':actual==expected,'source_crop_pixels_equal':crop_equal,'continuous':contiguous,
   'truth_v1_id':truthunit['id'] if truthunit else None,'truth_v1_lines':len(truthunit['lines']) if truthunit else 0,
   'baseline_candidates':matches,'baseline_reusable':baseline_reuse,'supplement_reusable':supplemental,
   'old_ocr_path':output.as_posix() if olddata else None,'old_ocr_image_hash_match':oldmatch,
   'old_ocr_regions':len(olddata['regions']) if olddata else None,
   'old_ocr_raw_characters':sum(len(x['text']) for x in olddata['regions']) if olddata else None,
   'old_product_sha':oldmeta['git_commit'] if oldmeta else None,'old_device':oldmeta['engine']['device'] if oldmeta else None})
 assert previous==orig['height'];assert im.size==(orig['width'],orig['height']);im.close()
 for rel,h in prov['origin_files'].items():assert digest(folder/rel)==h
 sources.append({'id':sid,'source_path':op.as_posix(),'source_sha256':orig['sha256'],'width':orig['width'],'height':orig['height'],
                 'sections':len(sp['sections']),'split_sha256':digest(folder/'split.json'),'provenance_sha256':digest(folder/'provenance.json')})
assert not errors,errors
summary={'sources':len(sources),'sections':len(rows),'bytes':sum(r['bytes'] for r in rows),'truth_reusable_sections':sum(bool(r['truth_v1_id']) for r in rows),
 'truth_reusable_lines':sum(r['truth_v1_lines'] for r in rows),'new_truth_sections':sum(not r['truth_v1_id'] for r in rows),
 'old_ocr_sections':sum(r['old_ocr_regions'] is not None for r in rows),'old_regions':sum(r['old_ocr_regions'] or 0 for r in rows),
 'old_characters':sum(r['old_ocr_raw_characters'] or 0 for r in rows),
 'baseline_reusable':sum(r['baseline_reusable'] for r in rows),'supplement_reusable':sum(r['supplement_reusable'] for r in rows),
 'required_new_outputs':sum(not (r['baseline_reusable'] or r['supplement_reusable']) for r in rows),
 'min_height':min(r['height'] for r in rows),'max_height':max(r['height'] for r in rows),
 'widths':sorted(set(r['width'] for r in rows)),'errors':errors}
out=W/'docs/ai-baseline/preparations/ocr-text-v2-preflight-20261105';out.mkdir(exist_ok=False)
write_new(out/'inventory.json',{'status':'preflight_only_not_frozen','source_dataset_sha256':digest(W/'pipeline/samples/evaluation/gs-v1/manifest.json'),
 'summary':summary,'sources':sources,'sections':rows})
print(summary)
for c in ['GS-01','GS-02','GS-03']:
 rr=[r for r in rows if r['source_id'].startswith(c)]
 print(c,len(rr),'reuse',sum(r['baseline_reusable'] or r['supplement_reusable'] for r in rr),'newtruth',sum(not r['truth_v1_id'] for r in rr))
