"""Plot saved keypad evaluation; no model loading, refitting or threshold changes.
Usage: python plot_results.py --models models --out comparison
Dependencies: numpy matplotlib scikit-learn
"""
import argparse
import csv
import json
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from sklearn.metrics import roc_curve, roc_auc_score

METHODS = ['svm', 'total', 'manhattan', 'diagonal']
LABELS = ['SVM', 'Total duration', 'Manhattan', 'Diagonal variance']
COLORS = ['#2563eb', '#94a3b8', '#0f766e', '#b45309']
METRICS = ['far', 'frr', 'balanced_accuracy', 'eer_approx']

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--models', default='models', type=Path)
    ap.add_argument('--out', default='comparison', type=Path)
    args = ap.parse_args()
    report = json.loads((args.models/'report.json').read_text(encoding='utf-8'))
    with (args.models/'test_predictions.csv').open(newline='', encoding='utf-8') as f:
        predictions = list(csv.DictReader(f))
    users = sorted(report['users'])
    grouped = {}
    for row in predictions:
        grouped.setdefault((row['claimed_user'], row['method']), []).append(row)
    # Recompute counts and decisions independently; fail if source files disagree.
    for user in users:
        for method in METHODS:
            rows = grouped[(user, method)]
            assert len({r['sample_id'] for r in rows}) == len(rows), 'Duplicate test comparisons'
            y = np.array([int(r['genuine']) for r in rows], dtype=bool)
            s = np.array([float(r['score']) for r in rows])
            accepted = np.array([int(r['accepted']) for r in rows], dtype=bool)
            model = report['users'][user][method]
            assert all(int(r['genuine']) == int(r['claimed_user']==r['actual_user']) for r in rows)
            assert all(float(r['threshold']) == model['threshold'] for r in rows)
            assert np.array_equal(accepted, s >= model['threshold'])
            stats = model['test']
            checks = dict(genuine_trials=int(y.sum()), impostor_trials=int((~y).sum()),
                          false_accepts=int((accepted & ~y).sum()),
                          false_rejects=int((~accepted & y).sum()))
            assert all(stats[k] == v for k,v in checks.items()), 'Counts disagree with report'
            assert np.isclose(stats['far'], checks['false_accepts']/checks['impostor_trials'])
            assert np.isclose(stats['frr'], checks['false_rejects']/checks['genuine_trials'])
            assert np.isclose(stats['auc'], roc_auc_score(y,s))
    args.out.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,
                         'axes.spines.top':False,'axes.spines.right':False,
                         'axes.titleweight':'bold','savefig.facecolor':'white'})
    def save(fig, name):
        fig.savefig(args.out/(name+'.png'), dpi=180, bbox_inches='tight')
        fig.savefig(args.out/(name+'.svg'), bbox_inches='tight')
        plt.close(fig)
    avg = {m:{k:100*np.mean([report['users'][u][m]['test'][k] for u in users])
              for k in METRICS} for m in METHODS}
    with (args.out/'comparison_summary.csv').open('w', newline='') as f:
        w=csv.writer(f); w.writerow(['method']+[k+'_percent' for k in METRICS]+['false_accepts','impostor_comparisons','false_rejects','genuine_comparisons'])
        for m in METHODS:
            w.writerow([m]+[avg[m][k] for k in METRICS]+[
                sum(report['users'][u][m]['test'][k] for u in users)
                for k in ['false_accepts','impostor_trials','false_rejects','genuine_trials']])
    fig, axes=plt.subplots(2,2,figsize=(12,8))
    for ax,k,title in zip(axes.flat,METRICS,['False acceptance rate | lower is better',
            'False rejection rate | lower is better','Balanced accuracy | higher is better',
            'Approximate EER | lower is better']):
        vals=[avg[m][k] for m in METHODS]
        bars=ax.bar(LABELS,vals,color=COLORS,width=.64)
        ax.bar_label(bars,labels=[f'{v:.2f}%' for v in vals],padding=4,fontsize=10)
        ax.set_ylim(0, max(vals)*1.25+0.3); ax.set_ylabel('Across-user mean (%)')
        ax.set_title(title,fontsize=11);ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True)
        ax.tick_params(axis='x',labelsize=9)
    fig.suptitle('Keypad authentication: S03 test comparison',fontsize=17,fontweight='bold')
    fig.text(.5,.015,'Thresholds fixed from S02. EER sweeps test thresholds for diagnosis only. Means give each user equal weight.',ha='center',fontsize=9)
    fig.tight_layout(rect=[0,.05,1,.94]);save(fig,'01_method_comparison')
    fig,axes=plt.subplots(1,2,figsize=(12,5.6))
    for ax,k,title in zip(axes,['far','frr'],['False acceptance rate (%)','False rejection rate (%)']):
        a=np.array([[report['users'][u][m]['test'][k]*100 for m in METHODS] for u in users])
        im=ax.imshow(a,cmap='YlOrRd',vmin=0,vmax=100,aspect='auto')
        ax.set_xticks(range(4),LABELS,rotation=15);ax.set_yticks(range(len(users)),users)
        ax.set_title(title)
        for (i,j),v in np.ndenumerate(a):
            ax.text(j,i,f'{v:.1f}',ha='center',va='center',color='white' if v>60 else '#111827')
        fig.colorbar(im,ax=ax,shrink=.8)
    fig.suptitle('Per-user errors at the deployed thresholds',fontsize=16,fontweight='bold')
    fig.text(.5,.015,'Each user: 30 genuine and 120 known-cohort impostor comparisons. Lower is better.',ha='center',fontsize=9)
    fig.tight_layout(rect=[0,.06,1,.93]);save(fig,'02_per_user_errors')
    fig,ax=plt.subplots(figsize=(10,5.5));x=np.arange(4)
    for offset,split,color,label in [(-.18,'calibration','#94a3b8','S02 calibration'),(.18,'test','#2563eb','S03 test')]:
        vals=[100*np.mean([report['users'][u][m][split]['frr'] for u in users]) for m in METHODS]
        bars=ax.bar(x+offset,vals,width=.34,label=label,color=color)
        ax.bar_label(bars,labels=[f'{v:.2f}%' for v in vals],padding=3)
    ax.set_xticks(x,LABELS);ax.set_ylabel('Mean false rejection rate (%)');ax.set_ylim(0,100)
    ax.set_title('Calibration performance did not fully carry over to S03')
    ax.legend(frameon=False);ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True)
    fig.text(.5,.01,'Calibration estimates are optimistic by construction: they were used to choose thresholds and the default method.',ha='center',fontsize=9)
    fig.tight_layout(rect=[0,.05,1,1]);save(fig,'03_calibration_vs_test')
    cols=2;nr=(len(users)+1)//2
    fig,axes=plt.subplots(nr,cols,figsize=(12,4*nr),squeeze=False)
    for ax,user in zip(axes.flat,users):
        for m,label,color in zip(METHODS,LABELS,COLORS):
            rows=grouped[(user,m)];y=[int(r['genuine']) for r in rows];s=[float(r['score']) for r in rows]
            fpr,tpr,_=roc_curve(y,s)
            ax.plot(fpr,tpr,label=f'{label} (AUC {roc_auc_score(y,s):.3f})',color=color,lw=1.8)
            stat=report['users'][user][m]['test']
            ax.scatter(stat['far'],1-stat['frr'],color=color,s=40,zorder=5,edgecolors='white',linewidths=.5)
        ax.plot([0,1],[0,1],ls='--',color='#cbd5e1',lw=1)
        ax.set(xlim=(-.02,1.02),ylim=(-.02,1.02),xlabel='False acceptance rate',ylabel='Genuine acceptance rate',title=user)
        ax.legend(loc='lower right',fontsize=8,framealpha=.9);ax.grid(alpha=.2)
    for ax in list(axes.flat)[len(users):]:ax.set_visible(False)
    fig.suptitle('Per-user ROC curves with calibrated operating points',fontsize=16,fontweight='bold')
    fig.text(.5,.012,'Dots show the S02-selected thresholds on S03. Curves sweep thresholds diagnostically; scores are not pooled across users.',ha='center',fontsize=9)
    fig.tight_layout(rect=[0,.04,1,.96]);save(fig,'04_roc_curves')
    print(json.dumps(avg,indent=2))
    print(f'Validated {len(predictions)} comparisons across {len({r["sample_id"] for r in predictions})} unique attempts; saved plots to {args.out}')

if __name__=='__main__':
    main()
