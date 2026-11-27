"""모델·DB에 접근하지 않는 Windows 부모 종료 재현용 작업."""
import argparse
import json
import multiprocessing
import os
from pathlib import Path
import time
from .windows_supervisor import identity


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def child(out):
    out=Path(out);write(out/'probe-child-started.json',dict(identity(),at=time.time()))
    print('probe child started',os.getpid(),flush=True)
    for i in range(20):
        time.sleep(.5);write(out/'probe-child-heartbeat.json',{'sequence':i,'at':time.time()})
    write(out/'probe-child-complete.json',dict(identity(),at=time.time()))
    print('probe child complete',flush=True)


def payload(out,native=False):
    write(out/'probe-worker-started.json',dict(identity(),at=time.time()))
    if native:
        os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK']='True'
        os.environ['HF_HUB_OFFLINE']='1'
        import torch
        import paddle
        paddle.set_device('cpu')
        # CPU 배열 연산만: 모델 가중치·추론·API 없음.
        assert float(torch.tensor([1.0]).sum())==1
        assert float(paddle.to_tensor([1.0]).sum())==1
        write(out/'probe-native.json',{'torch':torch.__version__,'paddle':paddle.__version__,'model_calls':0})
    p=multiprocessing.get_context('spawn').Process(target=child,args=(str(out),))
    p.start();p.join(40)
    if p.is_alive():raise RuntimeError('probe child timeout')
    write(out/'probe-worker-complete.json',dict(identity(),child_pid=p.pid,child_exit_code=p.exitcode,at=time.time()))
    if p.exitcode:raise RuntimeError('probe child failed')
    print('probe worker complete',flush=True)


def controller(out,request):
    write(out/'controller.json',identity())
    limit=time.monotonic()+30
    while not (out/'controller-go').exists():
        if time.monotonic()>limit:raise RuntimeError('controller gate timeout')
        time.sleep(.1)
    from .gs03_scheduled import task_action
    result=task_action(request)
    write(out/'controller-submitted.json',{'result':json.loads(result),'at':time.time()})
    while not (out/'controller-normal-exit').exists():time.sleep(.1)


if __name__=='__main__':
    multiprocessing.freeze_support()
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['payload','controller'])
    parser.add_argument('output',type=Path);parser.add_argument('--request',type=Path);parser.add_argument('--native',action='store_true')
    args=parser.parse_args()
    if args.mode=='payload':payload(args.output,args.native)
    else:controller(args.output,args.request)
