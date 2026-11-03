"""LPC1768 fixed-study-PIN collection and per-user verification on a PC.
Use --help. Python 3.10+. Firmware UART: 9600 8N1; DOWN/UP/PIN/*_US lines.
"""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import sys
from datetime import datetime, timezone
import uuid

FEATURES = [f"dwell{i}_us" for i in range(1, 5)] + [f"flight{i}_us" for i in range(1, 4)]
EVENT_COLS = [name for i in range(1, 5) for name in (f"down{i}_us", f"up{i}_us")]
FIELDS = ["sample_id", "user_id", "session_id", "capture_id", "captured_utc", "study_pin", "scan_us", "debounce_us"] + FEATURES + ["total_us"] + EVENT_COLS
METHODS = ("svm", "total", "manhattan", "diagonal")
MOD = 2**32

def utc():
    return datetime.now(timezone.utc).isoformat()

def delta(a, b):
    return (a - b) % MOD

class AttemptParser:
    """Fail closed on incomplete/malformed/incorrectly computed UART attempts."""
    def __init__(self):
        self.settings = None
        self.reset()
    def reset(self):
        self.events = []
        self.pin = None
        self.dwell = None
        self.flight = None
    def feed(self, line):
        line = line.strip()
        if not line or line.startswith('#'):
            return None
        if line.startswith('KEYPAD UART READY'):
            self.settings = None
            self.reset()
            return None
        if line.startswith('SCAN_US,'):
            self.reset()
            self.settings = None
            p = line.split(',')
            if len(p) != 4 or p[2] != 'DEBOUNCE_US':
                raise ValueError('Malformed firmware configuration')
            settings = (int(p[1]), int(p[3]))
            if min(settings) <= 0:
                raise ValueError('Invalid scan/debounce interval')
            self.settings = settings
            return None
        if line.startswith(('ERROR,', 'CLEAR')):
            self.reset()
            raise ValueError(line)
        p = line.split(',')
        try:
            if p[0] in ('DOWN', 'UP'):
                if len(p) != 3 or len(p[1]) != 1:
                    raise ValueError('Malformed event')
                t = int(p[2])
                if not 0 <= t < MOD:
                    raise ValueError('Timestamp outside uint32 range')
                if p[1] == '*':
                    self.reset()
                    return None
                if p[1] not in '0123456789':
                    self.reset()
                    raise ValueError('Non-digit key: discard partial entry and press *')
                self.events.append((p[0], p[1], t))
                if len(self.events) > 8:
                    raise ValueError('More than four keys; press * and retry')
            elif p[0] == 'PIN':
                if len(p) != 2 or len(p[1]) != 4 or not p[1].isdigit():
                    raise ValueError('Malformed four-digit PIN')
                self.pin = p[1]
            elif p[0] == 'DWELL_US':
                if self.pin is None or len(p) != 5:
                    raise ValueError('Missing PIN or malformed dwell line')
                self.dwell = [int(x) for x in p[1:]]
            elif p[0] == 'FLIGHT_US':
                if self.dwell is None or len(p) != 4:
                    raise ValueError('Missing dwell or malformed flight line')
                self.flight = [int(x) for x in p[1:]]
            elif p[0] == 'TOTAL_US':
                if len(p) != 2:
                    raise ValueError('Malformed total line')
                result = self.finish(int(p[1]))
                self.reset()
                return result
        except (ValueError, TypeError, IndexError):
            self.reset()
            raise
        return None
    def finish(self, total):
        if self.settings is None:
            raise ValueError('Missing SCAN_US banner: press RESET after opening logger')
        if self.pin is None or self.dwell is None or self.flight is None or len(self.events) != 8:
            raise ValueError('Incomplete attempt: key events and full summary required')
        ds, us = [], []
        for i in range(4):
            d, u = self.events[2*i:2*i+2]
            if d[:2] != ('DOWN', self.pin[i]) or u[:2] != ('UP', self.pin[i]):
                raise ValueError('Key order does not match completed PIN')
            ds.append(d[2]); us.append(u[2])
        dw = [delta(us[i], ds[i]) for i in range(4)]
        fl = [delta(ds[i+1], us[i]) for i in range(3)]
        if dw != self.dwell or fl != self.flight or delta(us[-1], ds[0]) != total:
            raise ValueError('Firmware features disagree with raw event timestamps')
        if min(dw) <= 0 or min(fl) < 0 or not 0 < total < 15_000_000 or sum(dw+fl) != total:
            raise ValueError('Invalid chronology, duration, or timeout')
        return dict(study_pin=self.pin, scan_us=self.settings[0], debounce_us=self.settings[1],
                    **dict(zip(FEATURES, dw+fl)), total_us=total,
                    **dict(zip(EVENT_COLS, [x for pair in zip(ds, us) for x in pair])))

def load_rows(path):
    path = Path(path)
    if not path.exists() or not path.stat().st_size:
        return []
    with path.open(newline='', encoding='utf-8') as f:
        r = csv.DictReader(f)
        if r.fieldnames != FIELDS:
            raise ValueError('CSV schema differs; use a new dataset file')
        return list(r)

def make_row(attempt, user, session, capture):
    row = dict(attempt, user_id=user, session_id=session, capture_id=capture, captured_utc=utc())
    identity = [user, session, attempt['study_pin'], attempt['scan_us'], attempt['debounce_us']] + [attempt[k] for k in EVENT_COLS]
    row['sample_id'] = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
    return row

def serial_lines(port, baud):
    try:
        import serial
    except ImportError as e:
        raise ValueError('Install dependencies: python -m pip install -r requirements.txt') from e
    # Open only after disabling handshaking. SW21 OFF and JP3 open on the kit.
    s = serial.Serial(port=None, baudrate=baud, timeout=1, bytesize=8, parity='N', stopbits=1,
                      xonxoff=False, rtscts=False, dsrdtr=False)
    s.dtr = False; s.rts = False; s.port = port
    try:
        s.open()
    except serial.SerialException as e:
        raise ValueError(f'Cannot open {port}. Close PuTTY/Flash Magic and check COM port: {e}') from e
    print('Connected. Press kit RESET now. SW21 OFF; JP3 open. Ctrl+C stops.')
    pending = b''
    try:
        while True:
            chunk = s.read_until(b'\n', size=4096)
            if not chunk:
                continue
            pending += chunk
            if len(pending) > 8192:
                pending = b''
                yield 'ERROR,OVERSIZED_SERIAL_LINE\n'
            elif pending.endswith(b'\n'):
                try:
                    yield pending.decode('ascii')
                except UnicodeDecodeError:
                    yield 'ERROR,NON_ASCII_SERIAL_DATA_CHECK_BAUD\n'
                pending = b''
    finally:
        s.close()

def collect(args):
    rows = load_rows(args.csv)
    seen = {r['sample_id'] for r in rows}
    same = [r for r in rows if r['user_id'] == args.user and r['session_id'] == args.session]
    if any(r['study_pin'] != args.pin for r in rows):
        raise ValueError('Use a separate CSV for each study PIN')
    saved = len(same)
    if args.command == 'collect' and saved >= args.samples:
        print(f'{args.user}/{args.session} already has {saved} samples; nothing added.')
        return
    capture = str(uuid.uuid4())
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    rawpath = Path(args.csv).parent / 'raw' / f'{args.user}_{args.session}_{stamp}_{capture[:8]}.log'
    rawpath.parent.mkdir(parents=True, exist_ok=True)
    parser = AttemptParser()
    rejected = 0
    warm = args.warmup or 0
    schema_settings = {(int(r['scan_us']), int(r['debounce_us'])) for r in rows}
    if len(schema_settings) > 1:
        raise ValueError('Existing dataset mixes timing settings')
    lines = serial_lines(args.port, args.baud) if args.command == 'collect' else Path(args.file).open(encoding='utf-8-sig', errors='replace')
    print(f'Actor={args.user}; session={args.session}; study PIN={args.pin}; already saved={saved}')
    print(f'Raw log: {rawpath}. First {warm} complete correct attempts are warm-up only.')
    try:
        with rawpath.open('x', encoding='utf-8') as raw, Path(args.csv).open('a', newline='', encoding='utf-8') as out:
            writer = csv.DictWriter(out, fieldnames=FIELDS)
            if out.tell() == 0:
                writer.writeheader(); out.flush()
            raw.write('# META ' + json.dumps(dict(user=args.user, session=args.session, capture=capture, study_pin=args.pin, warmup=warm)) + '\n')
            for line in lines:
                raw.write(line if line.endswith('\n') else line+'\n'); raw.flush()
                if args.command == 'import-log' and line.startswith('# META '):
                    meta = json.loads(line[len('# META '):])
                    if meta.get('user') != args.user or meta.get('session') != args.session:
                        raise ValueError('Log metadata does not match user/session; refusing to relabel')
                    if args.warmup is None:
                        warm = int(meta.get('warmup', 0))
                if args.command == 'collect':
                    print(line.rstrip())
                try:
                    a = parser.feed(line)
                except (ValueError, TypeError, IndexError) as e:
                    rejected += 1; print('DISCARDED:', e); continue
                if a is None:
                    continue
                if a['study_pin'] != args.pin:
                    rejected += 1; print('DISCARDED: wrong study PIN'); continue
                settings = (a['scan_us'], a['debounce_us'])
                if schema_settings and settings not in schema_settings:
                    raise ValueError('Timing settings changed; collect in a separate dataset')
                schema_settings.add(settings)
                if warm:
                    warm -= 1; print(f'WARM-UP discarded; {warm} left'); continue
                row = make_row(a, args.user, args.session, capture)
                if row['sample_id'] in seen:
                    print('SKIP duplicate attempt'); continue
                writer.writerow(row); out.flush(); seen.add(row['sample_id'])
                saved += 1
                print(f'SAVED {args.user}/{args.session}: {saved}; total_us={a["total_us"]}')
                if args.command == 'collect' and saved >= args.samples:
                    break
    except KeyboardInterrupt:
        print('\nStopped; previously saved rows are intact.')
    finally:
        if hasattr(lines, 'close'):
            lines.close()
    print(f'Session valid rows: {saved}; rejected messages/attempts: {rejected}; CSV: {args.csv}')

def validate_rows(rows):
    if not rows:
        raise ValueError('Dataset has no rows')
    ids = set(); fingerprints = set()
    for r in rows:
        if r['sample_id'] in ids:
            raise ValueError('Duplicate sample_id')
        ids.add(r['sample_id'])
        vals = [int(r[k]) for k in EVENT_COLS]
        if any(not 0 <= v < MOD for v in vals):
            raise ValueError('Raw timestamps outside uint32 range')
        if min(int(r['scan_us']), int(r['debounce_us'])) <= 0:
            raise ValueError('Invalid scan/debounce setting')
        fingerprint = tuple(vals + [int(r['scan_us']), int(r['debounce_us'])])
        if fingerprint in fingerprints:
            raise ValueError('Identical event trace reused across rows; check duplicated/relabelled logs')
        fingerprints.add(fingerprint)
        ds, us = vals[::2], vals[1::2]
        expected = [delta(us[i], ds[i]) for i in range(4)] + [delta(ds[i+1], us[i]) for i in range(3)]
        if expected != [int(r[k]) for k in FEATURES] or sum(expected) != int(r['total_us']):
            raise ValueError('Dataset timing features do not match raw events')
        if min(expected[:4]) <= 0 or not 0 < int(r['total_us']) < 15_000_000:
            raise ValueError('Invalid timing row')
    settings = {(r['study_pin'], int(r['scan_us']), int(r['debounce_us'])) for r in rows}
    if len(settings) != 1:
        raise ValueError('Train one dataset per study PIN and timing setting')
    return settings.pop()

def scores(model, x):
    import numpy as np
    if model['kind'] == 'svm':
        return model['pipeline'].decision_function(x)
    if model['kind'] == 'total':
        return -np.abs(x.sum(axis=1) - model['mean']) / model['scale']
    z = (x - model['mean']) / model['scale']
    return -np.mean(np.abs(z) if model['kind'] == 'manhattan' else z*z, axis=1)

def rates(y, s, threshold):
    import numpy as np
    genuine = np.asarray(y, dtype=bool)
    accepted = np.asarray(s) >= threshold
    ng, ni = int(genuine.sum()), int((~genuine).sum())
    if not ng or not ni:
        raise ValueError('Both genuine and impostor samples required in each split')
    fa, fr = int((accepted & ~genuine).sum()), int((~accepted & genuine).sum())
    return dict(genuine_trials=ng, impostor_trials=ni, false_accepts=fa, false_rejects=fr,
                far=fa/ni, frr=fr/ng, balanced_accuracy=1-(fa/ni+fr/ng)/2)

def choose_threshold(y, s, target_far):
    import numpy as np
    candidates = np.r_[np.unique(s), np.nextafter(np.max(s), np.inf)]
    choices = []
    for t in candidates:
        m = rates(y, s, t)
        if m['far'] <= target_far + 1e-12:
            choices.append((m['frr'], m['far'], float(t)))
    return min(choices)[2]

def train(args):
    import numpy as np
    import sklearn
    import joblib
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.svm import SVC
    from sklearn.metrics import roc_curve, roc_auc_score
    rows = load_rows(args.csv)
    pin, scan, debounce = validate_rows(rows)
    split_sets = dict(train=set(args.train_sessions), calibration=set(args.cal_sessions), test=set(args.test_sessions))
    if any(not x for x in split_sets.values()) or any(split_sets[a] & split_sets[b] for a,b in [('train','calibration'),('train','test'),('calibration','test')]):
        raise ValueError('Training/calibration/test session IDs must be nonempty and disjoint')
    if {r['session_id'] for r in rows} != set.union(*split_sets.values()):
        raise ValueError('Every CSV session must be assigned to exactly one split; check session arguments')
    users = sorted({r['user_id'] for r in rows})
    if len(users) < 2:
        raise ValueError('At least two users needed for supervised genuine/impostor models; five recommended')
    counts = {}
    captures = {}
    for split, sessions in split_sets.items():
        counts[split] = {u: sum(r['user_id']==u and r['session_id'] in sessions for r in rows) for u in users}
        if min(counts[split].values()) < args.min_samples:
            raise ValueError(f'{split}: need >= {args.min_samples} attempts per user; counts={counts[split]}')
        for r in rows:
            if r['session_id'] in sessions:
                key = r['capture_id']
                if key in captures and captures[key] != split:
                    raise ValueError('A capture spans splits; do not rename part of one session')
                captures[key] = split
    x = np.array([[int(r[f]) for f in FEATURES] for r in rows], dtype=float)
    actor = np.array([r['user_id'] for r in rows])
    masks = {name: np.array([r['session_id'] in sessions for r in rows]) for name,sessions in split_sets.items()}
    output = Path(args.out)
    if output.exists() and any(output.iterdir()):
        raise ValueError('Output directory is nonempty; choose a new --out to preserve prior evaluations')
    output.mkdir(parents=True, exist_ok=True)
    bundle = dict(schema_version=1, study_pin=pin, scan_us=scan, debounce_us=debounce,
                  features=FEATURES, users={}, sklearn_version=sklearn.__version__, created_utc=utc())
    report = dict(protocol='Known-user verification; session-disjoint splits; no unknown-user evaluation',
                  counts=counts, sessions={k: sorted(v) for k,v in split_sets.items()},
                  target_calibration_far=args.target_far, study_pin=pin, scan_us=scan, debounce_us=debounce,
                  features=FEATURES, dataset_sha256=hashlib.sha256(Path(args.csv).read_bytes()).hexdigest(),
                  sklearn_version=sklearn.__version__, users={})
    prediction_rows = []
    for user in users:
        y = (actor == user).astype(int)
        positive = x[masks['train'] & (y == 1)]
        scale = np.maximum(positive.std(axis=0, ddof=1), 1000.)
        mean = positive.mean(axis=0)
        pipe = make_pipeline(StandardScaler(), SVC(C=1.0, gamma='scale', kernel='rbf', class_weight='balanced'))
        pipe.fit(x[masks['train']], y[masks['train']])
        models = dict(svm=dict(kind='svm', pipeline=pipe),
                      total=dict(kind='total', mean=float(positive.sum(axis=1).mean()), scale=max(float(positive.sum(axis=1).std(ddof=1)),1000.)),
                      manhattan=dict(kind='manhattan', mean=mean, scale=scale),
                      diagonal=dict(kind='diagonal', mean=mean, scale=scale))
        report['users'][user] = {}
        for name, model in models.items():
            yc, yt = y[masks['calibration']], y[masks['test']]
            sc, st = scores(model, x[masks['calibration']]), scores(model, x[masks['test']])
            threshold = choose_threshold(yc, sc, args.target_far)
            model['threshold'] = threshold
            cal, test = rates(yc, sc, threshold), rates(yt, st, threshold)
            fpr, tpr, _ = roc_curve(yt, st)
            fnr = 1 - tpr
            j = int(np.argmin(np.abs(fpr-fnr)))
            test['eer_approx'] = float((fpr[j]+fnr[j])/2)
            test['eer_note'] = 'Closest empirical ROC point, diagnostic only; not an operating threshold'
            test['auc'] = float(roc_auc_score(yt, st))
            report['users'][user][name] = dict(threshold=threshold, calibration=cal, test=test)
            print(f'{user:8s} {name:10s} test FAR={test["far"]:.3f} FRR={test["frr"]:.3f} approximate EER={test["eer_approx"]:.3f}')
            for row, label, score in zip([r for r,m in zip(rows,masks['test']) if m], yt, st):
                prediction_rows.append(dict(sample_id=row['sample_id'], claimed_user=user, actual_user=row['user_id'],
                                            method=name, genuine=int(label), score=float(score), threshold=threshold,
                                            accepted=int(score>=threshold)))
        bundle['users'][user] = models
    # Calibration chooses a deployment method; test scores never select it here.
    selection = {}
    for method in METHODS:
        selection[method] = float(np.mean([report['users'][u][method]['calibration']['frr'] for u in users]))
    report['selected_method_from_calibration'] = min(selection, key=selection.get)
    bundle['default_method'] = report['selected_method_from_calibration']
    report['cautions'] = ['Repeated attempts are not independent participants.',
                          'Low observed FAR on a small dataset does not establish deployment security.',
                          'Other enrolled users supply negatives; unseen-user performance has not been tested.',
                          'Do not tune on these test results; reserve new sessions after making changes.']
    joblib.dump(bundle, output/'models.joblib')
    (output/'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    with (output/'test_predictions.csv').open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(prediction_rows[0])); w.writeheader(); w.writerows(prediction_rows)
    print(f'Saved models/report/predictions to {output}. Default method selected using calibration only: {bundle["default_method"]}')

def verify(args):
    import numpy as np
    import joblib
    import sklearn
    # Only load a model file you generated/trust: joblib is pickle based.
    bundle = joblib.load(args.model)
    if bundle['sklearn_version'] != sklearn.__version__:
        raise ValueError('Use the scikit-learn version recorded in report.json for this model')
    if args.user not in bundle['users']:
        raise ValueError('Unknown claimed user ID')
    method = args.method or bundle['default_method']
    model = bundle['users'][args.user][method]
    parser = AttemptParser()
    lines = serial_lines(args.port, args.baud) if args.port else Path(args.file).open(encoding='utf-8-sig', errors='replace')
    print(f'CLAIMED ID: {args.user}; method={method}; press study PIN on physical keypad.')
    try:
        for line in lines:
            try:
                a = parser.feed(line)
            except (ValueError, TypeError, IndexError) as e:
                print('INVALID ATTEMPT:', e); continue
            if a is None:
                continue
            if a['study_pin'] != bundle['study_pin']:
                print('REJECT: wrong study PIN'); continue
            if (a['scan_us'],a['debounce_us']) != (bundle['scan_us'],bundle['debounce_us']):
                print('INVALID: scan/debounce settings differ from training'); continue
            x = np.array([[a[f] for f in bundle['features']]], dtype=float)
            score = float(scores(model, x)[0]); threshold = model['threshold']
            print(f'{"ACCEPT" if score >= threshold else "REJECT"} {args.user}: score={score:.5f}; threshold={threshold:.5f} (score is not a probability)')
    except KeyboardInterrupt:
        print('\nStopped.')
    finally:
        if hasattr(lines, 'close'): lines.close()

def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    for name in ['collect','import-log']:
        s = sub.add_parser(name)
        s.add_argument('--user', required=True, help='Actual typist, e.g. U01')
        s.add_argument('--session', required=True, help='Real recording session, e.g. S01')
        s.add_argument('--pin', default='1234', help='Public dummy study PIN, not a real password')
        s.add_argument('--csv', default='data/dataset.csv')
        s.add_argument('--warmup', type=int, default=5 if name=='collect' else None)
        if name=='collect':
            s.add_argument('--port', required=True); s.add_argument('--baud', type=int, default=9600)
            s.add_argument('--samples', type=int, default=30, help='Target total valid rows for this user/session')
        else:
            s.add_argument('--file', required=True)
        s.set_defaults(func=collect)
    s = sub.add_parser('train')
    s.add_argument('--csv', default='data/dataset.csv')
    s.add_argument('--train-sessions', nargs='+', default=['S01'])
    s.add_argument('--cal-sessions', nargs='+', default=['S02'])
    s.add_argument('--test-sessions', nargs='+', default=['S03'])
    s.add_argument('--target-far', type=float, default=0.05, help='Calibration FAR ceiling; not a security guarantee')
    s.add_argument('--min-samples', type=int, default=10)
    s.add_argument('--out', default='models')
    s.set_defaults(func=train)
    s = sub.add_parser('verify')
    s.add_argument('--model', default='models/models.joblib')
    s.add_argument('--user', required=True, help='CLAIMED identity, regardless of who is actually typing')
    s.add_argument('--method', choices=METHODS)
    g=s.add_mutually_exclusive_group(required=True)
    g.add_argument('--port'); g.add_argument('--file')
    s.add_argument('--baud', type=int, default=9600)
    s.set_defaults(func=verify)
    args=p.parse_args()
    try:
        if hasattr(args,'pin') and (len(args.pin)!=4 or not args.pin.isascii() or not args.pin.isdigit()):
            raise ValueError('This firmware/toolkit requires a four-digit ASCII study PIN')
        for attr in ['user','session']:
            if hasattr(args,attr) and (not getattr(args,attr) or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in getattr(args,attr))):
                raise ValueError('Use only letters, digits, underscores and hyphens in user/session IDs')
        if hasattr(args,'warmup') and args.warmup is not None and args.warmup < 0:
            raise ValueError('warmup must be nonnegative')
        if hasattr(args,'samples') and args.samples < 1:
            raise ValueError('samples must be positive')
        if hasattr(args,'target_far') and not 0 <= args.target_far < 1:
            raise ValueError('target-far must be in [0,1)')
        if hasattr(args,'min_samples') and args.min_samples < 2:
            raise ValueError('min-samples must be at least 2')
        args.func(args)
    except (ValueError, OSError, ImportError, KeyError) as e:
        p.exit(1, f'ERROR: {e}\n')

if __name__ == '__main__':
    main()
