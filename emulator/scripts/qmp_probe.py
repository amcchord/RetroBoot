#!/usr/bin/env python3
"""Developer-only one-shot QMP probes; not a product management API."""
import argparse,json,socket,time,struct
from pathlib import Path

class QMP:
    def __init__(self,path):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(15);self.sock.connect(str(path));self.f=self.sock.makefile('rw');self.events=[];self.read();self.execute('qmp_capabilities')
    def read(self):
        while True:
            line=self.f.readline()
            if not line:raise ConnectionError('QMP disconnected')
            obj=json.loads(line)
            if 'event' in obj:self.events.append(obj);continue
            return obj
    def execute(self,command,**arguments):
        self.f.write(json.dumps({'execute':command,'arguments':arguments})+'\n');self.f.flush();r=self.read()
        if 'error' in r:raise RuntimeError(r['error'])
        return r.get('return')
    def close(self):self.f.close();self.sock.close()

def main():
    ap=argparse.ArgumentParser();ap.add_argument('run_dir',type=Path);sub=ap.add_subparsers(dest='action',required=True)
    sub.add_parser('status');p=sub.add_parser('text');p.add_argument('text');p=sub.add_parser('shot');p.add_argument('name');p=sub.add_parser('key');p.add_argument('qcodes',nargs='+');p=sub.add_parser('click');p.add_argument('x',type=int);p.add_argument('y',type=int);p.add_argument('--double',action='store_true');p.add_argument('--wait-exit',action='store_true');p=sub.add_parser('hmp');p.add_argument('command')
    args=ap.parse_args();args.run_dir=args.run_dir.resolve();q=QMP(args.run_dir/'qmp.sock');result=None;frame_dimensions=None
    try:
        if args.action=='status':result=q.execute('query-status')
        elif args.action=='shot':
            if '/' in args.name or not args.name.endswith('.png'):raise ValueError('Use a PNG basename')
            result=q.execute('screendump',filename=str(args.run_dir/args.name),format='png')
            width,height=struct.unpack('>II',(args.run_dir/args.name).read_bytes()[16:24]);frame_dimensions={'width':width,'height':height}
        elif args.action=='key':
            q.execute('send-key',keys=[{'type':'qcode','data':c} for c in args.qcodes],**{'hold-time':80})
            time.sleep(.15)
        elif args.action=='text':
            symbols={' ':'spc','\n':'ret','.':'dot','/':'slash','-':'minus',':':'semicolon'}
            if any(not (c.isascii() and (c.isalnum() or c in symbols)) for c in args.text):raise ValueError('Unsupported diagnostic text character')
            for char in args.text:
                codes=(['shift'] if char.isupper() or char==':' else [])+[symbols.get(char,char.lower())]
                q.execute('send-key',keys=[{'type':'qcode','data':c} for c in codes],**{'hold-time':50})
                time.sleep(.10)
        elif args.action=='click':
            frame=args.run_dir/'input-current.png'
            q.execute('screendump',filename=str(frame),format='png')
            width,height=struct.unpack('>II',frame.read_bytes()[16:24]);frame_dimensions={'width':width,'height':height}
            if not 0<=args.x<width or not 0<=args.y<height:raise ValueError('Pointer coordinate outside actual guest framebuffer')
            q.execute('input-send-event',events=[{'type':'abs','data':{'axis':'x','value':int(args.x*32767/(width-1))}},{'type':'abs','data':{'axis':'y','value':int(args.y*32767/(height-1))}}])
            time.sleep(.15)
            for _ in range(2 if args.double else 1):
                try:
                    q.execute('input-send-event',events=[{'type':'btn','data':{'button':'left','down':True}}]);time.sleep(.08)
                finally:q.execute('input-send-event',events=[{'type':'btn','data':{'button':'left','down':False}}])
                time.sleep(.1)
        elif args.action=='hmp':result=q.execute('human-monitor-command',**{'command-line':args.command})
        if args.action=='click' and args.wait_exit:
            try:
                while True:
                    line=q.f.readline()
                    if not line:break
                    event=json.loads(line)
                    if 'event' in event:q.events.append(event)
                    if event.get('event')=='SHUTDOWN':break
            except socket.timeout:
                result={'exit_event':'timeout'}
        record={'monotonic':time.monotonic(),'action':vars(args)|{'run_dir':str(args.run_dir)},'result':result,'events':q.events,'frame_dimensions':frame_dimensions}
        with (args.run_dir/'actions.jsonl').open('a') as f:f.write(json.dumps(record)+'\n')
        print(json.dumps(record))
    finally:q.close()

if __name__=='__main__':main()
