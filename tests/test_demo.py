import csv
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from demo_lcd import DemoProtocol, evaluate, load_bundle

ROOT=Path(__file__).resolve().parents[1]

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle=load_bundle(ROOT/'models/models.joblib')
        with (ROOT/'data/dataset.csv').open() as f: cls.rows=list(csv.DictReader(f))
        with (ROOT/'models/test_predictions.csv').open() as f: cls.pred=list(csv.DictReader(f))
    def attempt(self,row):
        return {k:(v if k=='study_pin' else int(v)) for k,v in row.items()
                if k in self.bundle['features']+['scan_us','debounce_us','study_pin','total_us']}
    def engine(self):
        e=DemoProtocol(self.bundle,'U01','svm')
        e.feed('KEYPAD UART READY - 9600 8N1');e.feed('SCAN_US,1000,DEBOUNCE_US,20000')
        cmd=e.feed('LCD_DEMO,1');self.assertTrue(cmd.endswith(',User 1\n'))
        e.feed('READY,'+e.token);self.assertTrue(e.ready)
        return e
    def feed_entry(self,e,row,pin='1234',bad=False):
        for i,k in enumerate(pin,1):
            e.feed(f'DOWN,{k},{row[f"down{i}_us"]}')
            e.feed(f'UP,{k},{row[f"up{i}_us"]}')
        e.feed(f'ATTEMPT,{e.token},1');e.feed('PIN,'+pin)
        e.feed('DWELL_US,'+','.join(row[f'dwell{i}_us'] for i in range(1,5)))
        e.feed('FLIGHT_US,'+','.join(row[f'flight{i}_us'] for i in range(1,4)))
        return e.feed('TOTAL_US,'+('1' if bad else row['total_us']))
    def test_saved_models_reproduce_all_test_decisions(self):
        rows={r['sample_id']:r for r in self.rows}
        for p in self.pred:
            r=evaluate(self.bundle,p['claimed_user'],p['method'],self.attempt(rows[p['sample_id']]))
            self.assertEqual(r['status']=='ACCEPT',bool(int(p['accepted'])))
            self.assertAlmostEqual(r['scores'][p['method']]['score'],float(p['score']),places=10)
    def test_protocol_valid_accept_reject_and_ack(self):
        byid={r['sample_id']:r for r in self.rows}
        for accepted in ['0','1']:
            p=next(p for p in self.pred if p['claimed_user']=='U01' and p['method']=='svm' and p['accepted']==accepted)
            e=self.engine();cmd=self.feed_entry(e,byid[p['sample_id']])
            status='ACCEPT' if accepted=='1' else 'REJECT'
            self.assertEqual(cmd,f'RESULT,{e.token},1,{status}\n')
            self.assertIsNone(e.feed('TOTAL_US,'+byid[p['sample_id']]['total_us']))
            e.feed(f'ACK,{e.token},1,{status}');self.assertIsNone(e.sent)
    def test_wrong_pin_settings_malformed(self):
        e=self.engine();self.assertTrue(self.feed_entry(e,self.rows[0],pin='9999').endswith(',REJECT\n'))
        e=self.engine();self.assertTrue(self.feed_entry(e,self.rows[0],bad=True).endswith(',INVALID\n'))
        a=self.attempt(self.rows[0]);a['scan_us']=2000
        self.assertEqual(evaluate(self.bundle,'U01','svm',a)['status'],'INVALID')
    def test_stale_frames_clear_and_reset(self):
        e=self.engine();token=e.token
        e.feed('ATTEMPT,badtoken,1');self.assertIsNone(e.attempt)
        e.feed(f'ATTEMPT,{token},1');e.feed('CLEAR');self.assertIsNone(e.attempt)
        e.feed(f'ATTEMPT,{token},1');self.assertIsNone(e.attempt)
        e.feed('KEYPAD UART READY - 9600 8N1');self.assertFalse(e.ready)
        e.feed('LCD_DEMO,1');self.assertNotEqual(e.token,token)
        e.feed('READY,'+token);self.assertFalse(e.ready)

if __name__=='__main__':unittest.main(verbosity=2)
