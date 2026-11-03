"""PC authentication with framed results sent back to LPC1768 LCD.
Run: python demo_lcd.py --port COM4 --user U01 --method svm
The supplied LCD-demo firmware must be flashed first. Reset after connecting.
"""
import argparse
import json
from pathlib import Path
import secrets
import time
from datetime import datetime, timezone
import numpy as np
from keystroke_ml import AttemptParser, FEATURES, METHODS, scores

def load_bundle(path):
    # Only load the trusted model you trained: joblib is pickle-based.
    import joblib
    import sklearn
    bundle = joblib.load(path)
    if bundle['sklearn_version'] != sklearn.__version__:
        raise ValueError(f"Model needs scikit-learn {bundle['sklearn_version']}; installed {sklearn.__version__}")
    if bundle['features'] != FEATURES:
        raise ValueError('Model feature order does not match collector')
    return bundle

def evaluate(bundle, user, method, attempt):
    result = dict(user=user, method=method, status='INVALID', reason='', scores={})
    if (attempt['scan_us'],attempt['debounce_us']) != (bundle['scan_us'],bundle['debounce_us']):
        result['reason'] = 'Scan/debounce differs from training'
        return result
    if attempt['study_pin'] != bundle['study_pin']:
        result.update(status='REJECT', reason='Wrong study PIN')
        return result
    x = np.array([[attempt[f] for f in FEATURES]], dtype=float)
    for name in METHODS:
        model = bundle['users'][user][name]
        score = float(scores(model,x)[0]); threshold = float(model['threshold'])
        if not np.isfinite(score) or not np.isfinite(threshold):
            raise ValueError('Non-finite model score or threshold')
        result['scores'][name] = dict(score=score, threshold=threshold, accepted=score>=threshold)
    result['status'] = 'ACCEPT' if result['scores'][method]['accepted'] else 'REJECT'
    result['reason'] = 'Timing comparison at saved calibration threshold'
    return result

class DemoProtocol:
    """Protocol engine separated from serial I/O for repeatable tests."""
    def __init__(self,bundle,user,method):
        if user not in bundle['users']: raise ValueError('Unknown user: '+user)
        if method not in METHODS: raise ValueError('Unknown method')
        self.bundle,self.user,self.method = bundle,user,method
        self.parser=AttemptParser();self.token=None;self.ready=False
        self.attempt=None;self.sent=None;self.last_id=0
        self.notice='Press RESET on the kit';self.last_result=None
    def feed(self,line):
        line=line.strip();self.notice='';self.last_result=None
        if line.startswith('KEYPAD UART READY'):
            self.token=None;self.ready=False;self.attempt=None;self.sent=None;self.last_id=0
        if line == 'LCD_DEMO,1':
            self.token=secrets.token_hex(8);self.ready=False;self.attempt=None;self.sent=None;self.last_id=0
            label=('User '+str(int(self.user[1:]))) if self.user.startswith('U') and self.user[1:].isdigit() else self.user
            if len(label)>11: raise ValueError('LCD label must fit 11 characters')
            return f'HELLO,{self.token},{label}\n'
        if line.startswith('READY,'):
            if self.token and line == 'READY,'+self.token:
                self.ready=True;self.notice=f'Ready: claimed {self.user}, method {self.method}. Enter PIN on keypad.'
            return None
        if line.startswith('ATTEMPT,'):
            p=line.split(',')
            if not self.ready or len(p)!=3 or p[1]!=self.token:
                self.notice='Ignored unrecognised attempt frame';return None
            try: n=int(p[2])
            except ValueError: return None
            if n<=self.last_id or n>0xFFFFFFFF:
                self.notice='Ignored stale attempt frame';return None
            self.last_id=n;self.attempt=n;return None
        if line.startswith('ACK,'):
            if self.sent and line == f'ACK,{self.token},{self.sent[0]},{self.sent[1]}':
                self.notice=f'LCD acknowledged {self.sent[1]} for attempt {self.sent[0]}'
                self.sent=None
            return None
        try:
            a=self.parser.feed(line)
        except (ValueError,TypeError,IndexError) as e:
            self.notice='INVALID: '+str(e)
            if line.startswith('TOTAL_US,') and self.ready and self.attempt is not None:
                return self.reply(dict(user=self.user,method=self.method,status='INVALID',reason=str(e),scores={}))
            self.attempt=None
            if line.startswith(('CLEAR','ERROR,')): self.sent=None
            return None
        if a is None: return None
        if not self.ready or self.attempt is None:
            self.notice='No matching demo frame: use new firmware and press RESET';return None
        return self.reply(evaluate(self.bundle,self.user,self.method,a))
    def reply(self,result):
        n=self.attempt;self.attempt=None
        result.update(attempt_id=n,utc=datetime.now(timezone.utc).isoformat())
        self.last_result=result;self.sent=(n,result['status'])
        return f"RESULT,{self.token},{n},{result['status']}\n"

def main():
    import serial
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--port',required=True)
    ap.add_argument('--user',required=True,help='Claimed account, e.g. U01; this is not automatic identification')
    ap.add_argument('--method',choices=METHODS,default=None)
    ap.add_argument('--model',type=Path,default=Path('models/models.joblib'))
    ap.add_argument('--logs',type=Path,default=Path('demo_logs'))
    args=ap.parse_args()
    bundle=load_bundle(args.model);method=args.method or bundle['default_method']
    engine=DemoProtocol(bundle,args.user,method)
    args.logs.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+secrets.token_hex(3)
    serial_port=serial.Serial(port=None,baudrate=9600,bytesize=8,parity='N',stopbits=1,
                             timeout=.1,write_timeout=2,xonxoff=False,rtscts=False,dsrdtr=False)
    serial_port.dtr=False;serial_port.rts=False;serial_port.port=args.port
    try:
        serial_port.open()
        print(f'Connected {args.port}. Claimed account: {args.user}; decision method: {method}.')
        print('SW21 OFF, JP3 open. Press RESET now with all keys released.')
        print('Wait for READY before typing. Press * for another attempt. Ctrl+C exits.')
        deadline=None;pending=b''
        with (args.logs/(stamp+'.jsonl')).open('x',encoding='utf-8') as log, (args.logs/(stamp+'.uart.log')).open('x',encoding='utf-8') as raw:
            while True:
                chunk=serial_port.read_until(b'\n',size=4096)
                if chunk:
                    pending+=chunk
                    if len(pending)>8192:
                        raise ValueError('Serial line too long; check firmware/baud and reset')
                    if pending.endswith(b'\n'):
                        line=pending.decode('ascii').strip();pending=b''
                        raw.write('RX '+line+'\n');raw.flush()
                        command=engine.feed(line)
                        if engine.notice: print(engine.notice)
                        if command:
                            serial_port.write(command.encode('ascii'));serial_port.flush()
                            raw.write('TX '+command);raw.flush()
                            deadline=time.monotonic()+4
                        if engine.last_result:
                            r=engine.last_result
                            print(f"{r['status']} {args.user}: {r['reason']}")
                            for name,s in r['scores'].items():
                                print(f"  {name:10s} {'ACCEPT' if s['accepted'] else 'REJECT':6s} score={s['score']:.5f} threshold={s['threshold']:.5f}")
                            log.write(json.dumps(r)+'\n');log.flush()
                        if engine.ready and engine.sent is None: deadline=None
                if deadline and time.monotonic()>deadline:
                    raise ValueError('No board acknowledgement. Check PC-to-kit RX path and reset; no confirmed LCD result.')
    except KeyboardInterrupt:
        print('\nStopped. No models or thresholds were changed.')
    finally:
        serial_port.close()

if __name__=='__main__':
    try: main()
    except (ValueError,OSError,ImportError,KeyError) as e:
        raise SystemExit('ERROR: '+str(e))
