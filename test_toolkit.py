"""Software checks use synthetic fixtures ONLY, not research accuracy evidence."""
import csv
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import random
import tempfile
from types import SimpleNamespace
import unittest

import keystroke_ml as k


def make_log(pin='1234', start=1000000, dwell=(120000,130000,110000,125000), flight=(80000,90000,70000), banner=True):
    s = ['KEYPAD UART READY - 9600 8N1','SCAN_US,1000,DEBOUNCE_US,20000'] if banner else []
    t=start
    for i,key in enumerate(pin):
        s.append(f'DOWN,{key},{t % k.MOD}')
        t+=dwell[i]
        s.append(f'UP,{key},{t % k.MOD}')
        if i<3: t+=flight[i]
    s.extend(['PIN,'+pin,'DWELL_US,'+','.join(map(str,dwell)),
              'FLIGHT_US,'+','.join(map(str,flight)), 'TOTAL_US,'+str(t-start)])
    return s


def parse(lines):
    p=k.AttemptParser(); result=None
    for line in lines:
        got=p.feed(line)
        if got: result=got
    return result


class Checks(unittest.TestCase):
    def test_features_wrap_and_leading_zero(self):
        a=parse(make_log(pin='0011',start=k.MOD-10000))
        self.assertEqual(a['total_us'],725000)
        self.assertEqual(a['study_pin'],'0011')
        self.assertEqual(a['flight2_us'],90000)
    def test_reject_bad_features_and_missing_events(self):
        s=make_log(); s[-1]='TOTAL_US,1'
        with self.assertRaises(ValueError): parse(s)
        with self.assertRaises(ValueError): parse(make_log()[2:])
        s=make_log(); s.pop(3)
        with self.assertRaises(ValueError): parse(s)
    def test_reset_invalidates_incomplete_attempt(self):
        p=k.AttemptParser()
        for s in make_log()[:5]: p.feed(s)
        with self.assertRaises(ValueError): p.feed('ERROR,QUEUE_OVERFLOW')
        self.assertEqual(p.events,[])
        p.feed('KEYPAD UART READY')
        self.assertIsNone(p.settings)
    def test_threshold_constraint_and_reject_all(self):
        import numpy as np
        y=np.array([1,1,0,0])
        s=np.array([.1,.2,.8,.9])
        t=k.choose_threshold(y,s,0)
        m=k.rates(y,s,t)
        self.assertEqual(m['far'],0)
        self.assertEqual(m['frr'],1)
    def test_import_replay_duplicate_and_relabel(self):
        with tempfile.TemporaryDirectory() as td:
            td=Path(td); f=td/'recording.log'
            f.write_text('\n'.join(make_log())+'\n')
            a=SimpleNamespace(command='import-log',csv=str(td/'dataset.csv'),user='U01',session='S01',pin='1234',file=str(f),warmup=None)
            with redirect_stdout(io.StringIO()): k.collect(a); k.collect(a)
            self.assertEqual(len(k.load_rows(a.csv)),1)
            f.write_text('# META '+json.dumps(dict(user='U02',session='S01'))+'\n')
            with self.assertRaises(ValueError), redirect_stdout(io.StringIO()): k.collect(a)
    def test_end_to_end_training_and_no_test_fit(self):
        import numpy as np
        import joblib
        rng=random.Random(7); rows=[]; counter=0
        with tempfile.TemporaryDirectory() as td:
            td=Path(td)
            for session in ('S01','S02','S03'):
                for ui in range(3):
                    for _ in range(12):
                        counter+=1
                        dw=[rng.randint(85000,180000)+ui*17000 for i in range(4)]
                        fl=[rng.randint(60000,140000)+ui*9000 for i in range(3)]
                        a=parse(make_log(start=counter*5000000,dwell=dw,flight=fl))
                        rows.append(k.make_row(a,f'U0{ui+1}',session,f'capture_{session}_{ui}'))
            f=td/'data.csv'
            def write():
                with f.open('w',newline='') as out:
                    w=csv.DictWriter(out,fieldnames=k.FIELDS); w.writeheader(); w.writerows(rows)
            write()
            a=SimpleNamespace(csv=str(f),train_sessions=['S01'],cal_sessions=['S02'],test_sessions=['S03'],min_samples=10,target_far=.05,out=str(td/'model1'))
            with redirect_stdout(io.StringIO()): k.train(a)
            b1=joblib.load(td/'model1/models.joblib')
            self.assertEqual(set(b1['users']),{'U01','U02','U03'})
            report=json.loads((td/'model1/report.json').read_text())
            self.assertEqual(report['counts']['test']['U01'],12)
            for r in rows:
                if r['session_id']=='S03':
                    dw=[int(r[x])+10000 for x in k.FEATURES[:4]]
                    fl=[int(r[x])+7000 for x in k.FEATURES[4:]]
                    updated=parse(make_log(start=int(r['down1_us']),dwell=dw,flight=fl))
                    r.update(updated)
            write(); a.out=str(td/'model2')
            with redirect_stdout(io.StringIO()): k.train(a)
            b2=joblib.load(td/'model2/models.joblib')
            for u in b1['users']:
                for method in k.METHODS:
                    self.assertEqual(b1['users'][u][method]['threshold'],b2['users'][u][method]['threshold'])
                np.testing.assert_array_equal(b1['users'][u]['svm']['pipeline'][0].mean_,b2['users'][u]['svm']['pipeline'][0].mean_)
                np.testing.assert_array_equal(b1['users'][u]['svm']['pipeline'][1].support_vectors_,b2['users'][u]['svm']['pipeline'][1].support_vectors_)
            a.cal_sessions=['S01']
            with self.assertRaises(ValueError), redirect_stdout(io.StringIO()): k.train(a)
            rows.append(dict(rows[0])); write()
            with self.assertRaises(ValueError): k.validate_rows(k.load_rows(f))

if __name__=='__main__': unittest.main(verbosity=2)
