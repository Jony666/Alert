"""Predeclared validation-only ranking; simultaneous intervals are exploratory."""
from statistics import NormalDist
from datetime import datetime
import math
import numpy as np
from ..research import stats, wilson
from ..data import timestamp
from ..config import DAY

FAMILY=24 # eight strategies x three primary timeframes, frozen before this run

def interval(wins,n,alpha=.05,family=FAMILY):
    if n==0:return [0.,1.]
    z=NormalDist().inv_cdf(1-alpha/(2*family));p=wins/n;den=1+z*z/n
    center=(p+z*z/(2*n))/den
    half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return [max(0.,center-half),min(1.,center+half)]

def summarize(trades):
    net=np.array([t['net_twd'] for t in trades])
    holds=np.array([t['hold_days'] for t in trades])
    proxy=[];next_allowed=-1
    # Greedy, outcome-blind thinning across ALL assets. At least 14d between
    # entry times AND prior chosen trade has exited; still not IID proof.
    for t in sorted(trades,key=lambda t:(t['entry_time'],t['symbol'])):
        entry=timestamp(t['entry_time'])
        if entry>=next_allowed:
            proxy.append(t);next_allowed=max(entry+14*DAY,timestamp(t['exit_time']))
    n=len(trades);wins=int((net>0).sum())
    return {**stats(trades),'win_ci_95':wilson(wins,n),
            'simultaneous_ci_95_family24':interval(wins,n),
            'avg_win_twd':float(net[net>0].mean()) if (net>0).any() else None,
            'avg_loss_twd':float(net[net<0].mean()) if (net<0).any() else None,
            'median_hold_days':float(np.median(holds)) if n else None,
            'max_hold_days':float(holds.max()) if n else None,
            'share_hold_1_to_3_days':float(np.mean((holds>=1)&(holds<=3))) if n else None,
            'nonoverlap_14d_proxy_n':len(proxy),
            'nonoverlap_14d_proxy_ci':interval(sum(t['net_twd']>0 for t in proxy),len(proxy)),
            'independence_verified':False,'pristine_test':False,'forward_verified':False,
            'qualification':'NO QUALIFIED SIGNAL',
            'uncertainty':'Repeatedly observed 2022–2025; Bonferroni/Wilson assumes IID within sample. Thinning is a proxy, not independence proof.'}

def rank(rows):
    """Never reads test_2025 rows. No threshold was chosen after results."""
    output=[]
    keys=sorted({(r['bar'],r['strategy']) for r in rows if r['strategy']!='ALL'})
    for bar,strat in keys:
        val=[r for r in rows if r['bar']==bar and r['strategy']==strat and r['split'].startswith('wf_')]
        base=[r for r in val if r['cost_mult']==1 and r['execution']=='base']
        stress=[r for r in val if r['cost_mult']==2]
        n=sum(r['trades'] for r in base)
        negative_crashes=sum(r['return_pct']<0 and r['max_drawdown_pct']>=20 for r in base)
        positive=sum(r['return_pct']>0 for r in base)
        regimes=sum(r.get('regime_count',0)>=2 for r in base)
        hold_good=sum(r['avg_hold_days'] is not None and 1<=r['avg_hold_days']<=3 for r in base)
        worst_dd=max((r['max_drawdown_pct'] for r in base),default=100.)
        cost_ok=bool(stress) and all(r['return_pct']>=0 for r in stress)
        lower=min((r['simultaneous_ci_95_family24'][0] for r in base),default=0.)
        proxy_min=min((r['nonoverlap_14d_proxy_n'] for r in base),default=0)
        reasons=[]
        if negative_crashes>=2:
            classification='淘汰';reasons.append('至少兩個驗證區塊負報酬且回撤達20%；僅淘汰本凍結版本，保留全部資料')
        elif n>=100 and proxy_min>=8 and positive>=3 and regimes>=3 and hold_good>=3 and worst_dd<20 and cost_ok and lower>=.60:
            classification='保留研究';reasons.append('通過預定研究保留門檻；仍缺前瞻及全倉認證，非合格進場')
        else:
            classification='需要改進'
        if n<100:reasons.append('驗證交易總數不足100，且重疊／相關性仍待驗證')
        if proxy_min<8:reasons.append('各區塊非重疊14日代理樣本不足8')
        if lower<.60:reasons.append('多重比較校正勝率下界未達60%')
        if not cost_ok:reasons.append('雙倍成本未在所有驗證區塊保持非負報酬')
        if hold_good<3:reasons.append('至少兩個驗證區塊平均持倉不在1–3日')
        reasons.append('官方全倉風險與前瞻績效未驗證')
        output.append(dict(bar=bar,strategy=strat,classification=classification,reasons=reasons,
                           validation_trades=n,positive_validation_blocks=positive,
                           validation_worst_dd=worst_dd,hold_compliant_blocks=hold_good,
                           simultaneous_lower_min=lower,proxy_n_min=proxy_min,
                           joint_cross_stress_verified=False,
                           sort_key=[{'保留研究':0,'需要改進':1,'淘汰':2}[classification],-positive,-lower,-proxy_min,not cost_ok,worst_dd,-hold_good,bar,strat]))
    return sorted(output,key=lambda r:r['sort_key'])
