from collections import defaultdict
import numpy as np
from ..phase3.evaluation import summarize as old_summary,interval
from ..data import timestamp
from ..config import DAY
from .design import FAMILY,CORES,EXITS

def summarize(trades):
    r=old_summary(trades);n=len(trades);wins=sum(t['net_twd']>0 for t in trades)
    r['simultaneous_ci_family42']=interval(wins,n,family=FAMILY)
    # Do not expose inherited family24 as the Phase4 multiple-comparison claim.
    r.pop('simultaneous_ci_95_family24',None)
    holds=np.array([t['hold_days'] for t in trades]);rs=np.array([t['net_r'] for t in trades])
    r['hold_percentiles_days']={str(p):float(np.percentile(holds,p)) if n else None for p in (5,25,50,75,90,95,99)}
    r['net_r_p05']=float(np.percentile(rs,5)) if n else None
    r['worst_net_r']=float(rs.min()) if n else None
    r['worst_loss_twd']=min([t['net_twd'] for t in trades]+[0.])
    r['expectancy_twd_per_trade']=sum(t['net_twd'] for t in trades)/n if n else None
    clusters=defaultdict(list)
    for t in trades:clusters[timestamp(t['entry_time'])//(14*DAY)].append(t['net_r'])
    means=np.array([np.mean(v) for _,v in sorted(clusters.items())])
    r['calendar_14d_cluster_count']=len(means)
    if len(means)>=2:
        samples=np.random.default_rng(20261024).choice(means,(1000,len(means)),replace=True).mean(axis=1)
        r['exploratory_cluster_mean_r_ci']=list(map(float,np.percentile(samples,[2.5,97.5])))
    else:r['exploratory_cluster_mean_r_ci']=None
    r['effective_independent_n']=None
    r['independence_note']='14d calendar counts/thinning/block bootstrap are proxies; sparse correlated samples do not identify true effective N.'
    return r

def classify(rows):
    profiles=[]
    for core in CORES:
        for bar in ('1H','4H','1D'):
            for ex in EXITS:
                base=[r for r in rows if r['core']==core and r['bar']==bar and r['variant']=='base' and r['exit']==ex and r['split'].startswith('wf_') and r['pressure']=='base']
                stress=[r for r in rows if r['core']==core and r['bar']==bar and r['variant']=='base' and r['exit']==ex and r['split'].startswith('wf_') and r['pressure']=='double_cost']
                n=sum(r['trades'] for r in base);proxy=min((r['nonoverlap_14d_proxy_n'] for r in base),default=0)
                positive=sum(r['return_pct']>0 for r in base)
                lower=min((r['simultaneous_ci_family42'][0] for r in base),default=0.)
                dd=max((r['max_drawdown_pct'] for r in base),default=100.)
                hold=sum(r['avg_hold_days'] is not None and 1<=r['avg_hold_days']<=3 for r in base)
                stress_good=bool(stress) and all(r['return_pct']>=0 for r in stress)
                credible=n>=100 and proxy>=8
                if credible and positive>=3 and lower>=.60 and dd<20 and hold>=3 and stress_good:
                    category='A';reasons=['多區塊證據通過研究保留規則；仍非實盤資格']
                elif credible and positive<=1 and not stress_good:
                    category='C';reasons=['足量驗證下至少三區塊無正報酬且成本壓力無優勢；停止此凍結版本']
                else:
                    category='B';reasons=[]
                if not credible:reasons.append('樣本/14日代理不足，不能確證有效或永遠無效')
                if lower<.60:reasons.append('42比較校正下界未達60%')
                if not stress_good:reasons.append('雙倍成本未全面穩健')
                if hold<3:reasons.append('驗證持倉未在至少三區塊平均1–3日')
                reasons.append('2025受污染；尚待前瞻及官方全倉驗證')
                profiles.append(dict(core=core,bar=bar,exit=ex,category=category,reasons=reasons,validation_n=n,proxy_min=proxy,
                                     positive_blocks=positive,win_lower_min=lower,worst_dd=dd,hold_compliant_blocks=hold,
                                     double_cost_all_nonnegative=stress_good,
                                     sort_key=[{'A':0,'B':1,'C':2}[category],-proxy,-n,-positive,not stress_good,dd,-hold,core,bar,ex]))
    ranked=sorted(profiles,key=lambda x:x['sort_key'])
    decisions=[]
    for core in CORES:
        ps=[p for p in ranked if p['core']==core]
        eligible=[p for p in ps if p['category']=='A']
        category='A' if eligible else 'C' if all(p['category']=='C' for p in ps) else 'B'
        decisions.append(dict(core=core,category=category,chosen_exit=eligible[0]['exit'] if eligible else None,
                              chosen_bar=eligible[0]['bar'] if eligible else None,
                              decision='只在A條件成立時選擇後續紙上觀察版本；不以最高回測報酬挑退出',
                              qualified_for_live=False))
    return dict(profiles=ranked,core_decisions=decisions,selection_uses='wf_2023–2024 only; test_2025 never used')
