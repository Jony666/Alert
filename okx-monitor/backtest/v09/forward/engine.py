from copy import deepcopy
from dataclasses import asdict
import math
import pandas as pd
from ..config import STEPS,DAY,CONFIG
from ..data import COLS,validate as validate_bars,timestamp,iso
from ..events import assess
from ..exits import Plan,costs,quantize_plan,bar_exit
from ..risk import State,size,day
from ..strategies import features
from ..phase3.strategies import enrich,signal,plan as strategy_plan
from .freeze import IDS,digest
from .state import event,validate,financial_hash


def check_snapshot(snap,freeze,allow_test=False):
    if snap['origin']!='REAL_OKX_PUBLIC' and not (allow_test and snap['origin']=='SYNTHETIC_TEST'):raise ValueError('synthetic data forbidden for real research')
    now=timestamp(snap['observed_at_utc'])
    expected={sym+'-USDT-SWAP|'+bar for sym in freeze['symbols'] for bar in freeze['bars']}
    if set(snap['frames'])!=expected:raise ValueError('incomplete universe snapshot')
    for key,rows in snap['frames'].items():
        inst,bar=key.split('|');step=STEPS[bar];validate_bars(pd.DataFrame(rows,columns=COLS),step)
        if rows[-1][0]+step!=now//step*step:raise ValueError('stale/unconfirmed latest candle')
        if len(rows)<80:raise ValueError('insufficient strategy context')
    for inst,m in snap['marks'].items():
        if not math.isfinite(m['price']) or m['price']<=0 or abs(now-m['time'])>300000:raise ValueError('mark stale/invalid')
    return now

def initialize(freeze,snap,run_id,run_at,allow_test=False):
    now=check_snapshot(snap,freeze,allow_test)
    if timestamp(run_at)<timestamp(freeze['freeze_timestamp_utc']):raise ValueError('research cannot predate freeze')
    s=dict(schema_version=1,study_id=freeze['study_id'],config_hash=freeze['config_hash'],book='PAPER_FORWARD',
           freeze=deepcopy(freeze),research_start_timestamp_utc=run_at,root_run_id=str(run_id),revision=1,
           last_success_at=run_at,last_success_run_id=str(run_id),last_processed_at=snap['observed_at_utc'],
           watermarks={k:rows[-1][0] for k,rows in snap['frames'].items()},
           cursor_hashes={k:digest(rows[-1]) for k,rows in snap['frames'].items()},
           instruments=deepcopy(snap['instruments']),marks=deepcopy(snap['marks']),
           candidates={},pending={},positions={},closed=[],journal=[],equity=[],
           cash_twd=10000.,expense_used_twd=0.,max_drawdown=0.,daily_halts={},risk=asdict(State()),
           last_snapshot_hash=digest(snap),last_snapshot_at=snap['observed_at_utc'],warnings=['NEWS_UNKNOWN','CROSS_NOT_VERIFIED','WIN_RATE_NOT_VERIFIED'])
    event(s,'INITIALIZE','genesis',dict(run_id=str(run_id),freeze_hash=s['config_hash'],research_start=run_at,watermarks=s['watermarks']))
    s['equity'].append(dict(time=snap['observed_at_utc'],cash_twd=10000.,unrealized_twd=0.,equity_twd=10000.,drawdown=0.))
    validate(s,freeze);return s

def exposures(s):
    rows={}
    for key,p in s['pending'].items():rows[key]=dict(strategy=p['core'],risk_twd=p['risk_twd'],notional_twd=p['notional_twd'])
    for key,p in s['positions'].items():
        if key not in rows:rows[key]=dict(strategy=p['core'],risk_twd=p['risk_twd'],notional_twd=p['units_total']*p['entry']*32)
    return list(rows.values())

def observe(s,at):
    u=sum(p['remaining_units']*p['side']*(s['marks'][p['symbol']]['price']-p['entry'])*32 for p in s['positions'].values())
    risk=State(**s['risk']);risk.capital=max(0,s['cash_twd']+u);risk.reserve=max(0,1000-s['expense_used_twd']);risk.positions=exposures(s)
    # No paper PnL is treated as verified real project capital for a risk step-up.
    risk.verified_capital=None;risk.observe();s['risk']=asdict(risk)
    s['max_drawdown']=max(s['max_drawdown'],1-(s['cash_twd']+u)/risk.peak)
    return risk,u

def qualification_reasons(freeze,snap,at,symbol):
    q=freeze['quality'];reasons=[]
    if not(q['n']>=100 and q['win_ci_lower']>=.60 and q['independence_verified'] and q['pristine_or_forward_verified']):reasons.append('WIN_RATE_NOT_VERIFIED')
    if not q['official_cross_verified']:reasons.append('CROSS_NOT_VERIFIED')
    if snap['news']['status']!='VERIFIED_COVERAGE':reasons.append('NEWS_UNKNOWN_NOT_NO_EVENT')
    if assess(snap['news'].get('events',[]),at,symbol)['requires_review']:reasons.append('MAJOR_EVENT_REVIEW')
    return reasons

def eligible_open(observed_ms,step):
    # A full future bar, never the earlier open of a just-observed closed candle.
    return (observed_ms//step+1)*step

def cancel_pending(s,key,reason):
    s['pending'].pop(key,None)
    if key in s['candidates']:s['candidates'][key]['state']='CANCELLED'
    event(s,'PENDING_CANCEL',key,{'reason':reason})

def close_piece(s,key,units,px,at,reason):
    p=s['positions'][key];units=min(units,p['remaining_units'])
    if units<=0:return
    fee=units*px*.0005*32;gain=units*p['side']*(px-p['entry'])*32
    eid=f'{key}|{at}|{reason}|{units:.12g}'
    if not event(s,'EXIT_FILL',eid,dict(units=units,price=px,fee_twd=fee,reason=reason,time=iso(at))):return
    s['cash_twd']+=gain-fee;s['expense_used_twd']+=fee;p['net_twd']+=gain-fee;p['fees_twd']+=fee
    p['slippage_twd']+=units*px/(1-p['side']*.0005)*.0005*32
    p['remaining_units']-=units;p['exits'].append(dict(time=iso(at),price=px,units=units,reason=reason,fee_twd=fee))
    # Never add more entry quantity after the first exit; cancel partial remainder.
    if key in s['pending']:
        cancel_pending(s,key,'EXIT_STARTED')
    if p['remaining_units']<=1e-10:
        p['exit_time']=iso(at);p['state']='CLOSED';p['net_r']=p['net_twd']/p['risk_twd'];p['hold_days']=(at-p['entry_ts'])/DAY
        s['closed'].append(p);del s['positions'][key]
        if key in s['candidates']:s['candidates'][key]['state']='CLOSED'
        s['risk']['closes'].append(dict(time=iso(at),strategy=p['core'],net=p['net_twd']))
        today=[c for c in s['risk']['closes'] if day(c['time'])==day(iso(at))]
        if len(today)>=2 and all(c['net']<0 for c in today[-2:]):s['daily_halts'][day(iso(at))]='TWO_LOSSES_NO_AUTOMATIC_EXCEPTION'
        event(s,'TRADE_CLOSED',key,dict(net_twd=p['net_twd'],net_r=p['net_r']))

def fill_pending(s,key,row,at,snap,parent_completed):
    order=s['pending'][key];step=STEPS[order['bar']]
    if at<order['eligible_open_ts']:return
    risk,_=observe(s,iso(at))
    if risk.halted or day(iso(at)) in s['daily_halts']:
        cancel_pending(s,key,'GLOBAL_OR_DAILY_HALT_AT_ENTRY');return
    if timestamp(parent_completed)>=order['eligible_open_ts']:
        cancel_pending(s,key,'NOT_DURABLE_BEFORE_ELIGIBLE_OPEN');return
    if at>=order['expires_at']:
        cancel_pending(s,key,'EXPIRED_UNFILLED_REMAINDER');return
    side=order['side'];spec=s['instruments'][order['symbol']]
    if order['execution']=='LIMIT_STRICT_CROSS_PARTIAL':
        # A mere touch is NOT a fill. Price must strictly cross the resting limit.
        if not row.low<order['expected_entry']<row.high:return
        px=order['expected_entry'];fraction=.5
    else:
        px=row.open*(1+side*.0005);fraction=1.
        if abs(px/order['expected_entry']-1)>.005:
            cancel_pending(s,key,'OUTSIDE_PRECOMMITTED_CONDITIONAL_ENTRY_BAND');return
    try:pplan=quantize_plan(Plan(px,order['stop'],order['tp1'],order['tp2'],side),spec['tickSz'])
    except ValueError:
        cancel_pending(s,key,'STRUCTURE_INVALID_AT_CONDITIONAL_OPEN');return
    c=costs(pplan)
    if c['net_rr']<3:
        cancel_pending(s,key,'NET_RR_AT_CONDITIONAL_OPEN_BELOW_3');return
    qty=min(order['remaining_units'],order['original_units']*fraction,row.quote_volume*.0001/px)
    lot=float(spec['ctVal'])*float(spec['lotSz']);qty=math.floor(qty/lot)*lot
    if qty<float(spec['ctVal'])*float(spec['minSz']):return
    # Rebound size at the precommitted conditional open; never exceed original budget.
    remaining_budget=max(0.,order['risk_twd']-s['positions'].get(key,{}).get('risk_twd',0.))
    qty=min(qty,math.floor((remaining_budget/(c['risk_per_unit']*32))/lot)*lot)
    if qty<=0:return
    fee=qty*pplan.entry*.0005*32
    if not event(s,'ENTRY_FILL',f'{key}|{at}',dict(time=iso(at),price=pplan.entry,units=qty,fee_twd=fee,ohlc_execution_uncertain=True)):return
    if key not in s['positions']:
        s['positions'][key]=dict(id=key,book='PAPER_FORWARD',core=order['core'],strategy_id=order['strategy_id'],
           strategy_version=order['strategy_version'],config_hash=s['config_hash'],code_commit_sha=order['code_commit_sha'],
           freeze_timestamp_utc=order['freeze_timestamp_utc'],research_start_timestamp_utc=s['research_start_timestamp_utc'],
           symbol=order['symbol'],bar=order['bar'],side=side,entry_ts=at,entry_time=iso(at),entry=pplan.entry,
           units_total=0.,remaining_units=0.,stop=pplan.stop,initial_stop=pplan.stop,tp1=pplan.tp1,tp2=pplan.tp2,
           tp1_done=False,fees_twd=0.,funding_twd=0.,slippage_twd=0.,net_twd=0.,risk_twd=0.,planned_net_rr=c['net_rr'],
           entries=[],exits=[],state='OPEN',regime=order['regime'],execution_model=order['execution'],live_qualified=False)
    p=s['positions'][key];total=p['units_total']+qty
    p['entry']=(p['entry']*p['units_total']+pplan.entry*qty)/total;p['units_total']=total;p['remaining_units']+=qty
    p['risk_twd']+=qty*c['risk_per_unit']*32;p['fees_twd']+=fee;p['net_twd']-=fee
    p['slippage_twd']+=qty*max(0.,p['side']*(pplan.entry-row.open))*32 if order['execution']!='LIMIT_STRICT_CROSS_PARTIAL' else 0.
    p['entries'].append(dict(time=iso(at),price=pplan.entry,units=qty,fee_twd=fee))
    s['cash_twd']-=fee;s['expense_used_twd']+=fee;order['remaining_units']-=qty
    if order['remaining_units']<lot:
        del s['pending'][key]
        if key in s['candidates']:s['candidates'][key]['state']='SIMULATED_ENTRY'
    else:
        p['state']='PARTIAL_ENTRY'
        if key in s['candidates']:s['candidates'][key]['state']='PARTIAL_ENTRY'

def position_bar(s,key,row,at,snap):
    p=s['positions'][key];step=STEPS[p['bar']];end=at+step
    if p['entry_ts']>at:return # never use pre-entry intrabar prices
    if at-p['entry_ts']>=DAY and getattr(row,'prev_close',None) is not None and (row.prev_close-p['initial_stop'])*p['side']<0:
        close_piece(s,key,p['remaining_units'],row.open*(1-p['side']*.0005),at,'STRUCTURE_INVALIDATION');return
    if at-p['entry_ts']>=3*DAY:
        close_piece(s,key,p['remaining_units'],row.open*(1-p['side']*.0005),at,'TIME_EXIT_3D');return
    for rate in snap['funding'].get(p['symbol'],[]):
        if p['entry_ts']<=rate['time']<=end:
            pay=p['remaining_units']*p['entry']*abs(rate['rate'])*32
            if event(s,'FUNDING',f"{key}|{rate['time']}",dict(time=iso(rate['time']),adverse_abs_public_rate=rate['rate'],twd=pay)):
                p['funding_twd']+=pay;p['net_twd']-=pay;s['cash_twd']-=pay;s['expense_used_twd']+=pay
    q=Plan(p['entry'],p['initial_stop'],p['tp1'],p['tp2'],p['side'])
    remaining=p['remaining_units']/p['units_total']
    exits=bar_exit(q,row.open,row.high,row.low,p['stop'],remaining,p['tp1_done'],.0005)
    for fraction,px,reason in exits:
        qty=min(p['remaining_units'],p['units_total']*fraction)
        close_piece(s,key,qty,px,end,reason)
        if key not in s['positions']:break
        if reason=='tp1':p['tp1_done']=True;p['state']='PARTIAL_EXIT'
    if key in s['positions'] and p['tp1_done']:
        trail=row.close-p['side']*2*row.atr
        p['stop']=max(p['stop'],p['entry'],trail) if p['side']==1 else min(p['stop'],p['entry'],trail)


def advance(previous,freeze,snap,run_id,run_at,parent_completed,allow_test=False):
    validate(previous,freeze,run_at);now=check_snapshot(snap,freeze,allow_test);s=deepcopy(previous)
    if timestamp(snap['observed_at_utc'])<timestamp(previous['last_snapshot_at']) and digest(snap)!=previous['last_snapshot_hash']:raise ValueError('snapshot time rollback')
    for inst,spec in snap['instruments'].items():
        if any(spec.get(k)!=s['instruments'][inst].get(k) for k in ('ctVal','ctValCcy','lotSz','minSz','tickSz','lever')):raise ValueError('contract specification changed; review/version required')
    for p in list(previous['positions'].values())+list(previous['pending'].values()):
        if p['symbol'] not in snap['funding']:raise ValueError('funding coverage missing for existing exposure')
    duplicate=digest(snap)==previous['last_snapshot_hash']
    if not duplicate:
        tables={};events=[];latest=[];btc={}
        for bar in freeze['bars']:
            btc[bar]=features(pd.DataFrame(snap['frames']['BTC-USDT-SWAP|'+bar],columns=COLS),STEPS[bar])
        for key,rows in snap['frames'].items():
            inst,bar=key.split('|');step=STEPS[bar];cursor=s['watermarks'][key]
            if rows[-1][0]<cursor:raise ValueError('candle cursor rollback')
            old=[r for r in rows if r[0]==cursor]
            if not old or digest(old[0])!=s['cursor_hashes'][key]:raise ValueError('cursor candle changed or missing')
            x=enrich(pd.DataFrame(rows,columns=COLS),step,pd.DataFrame(snap['frames']['BTC-USDT-SWAP|'+bar],columns=COLS),btc[bar])
            x['prev_close']=x.close.shift()
            tables[key]=list(x.itertuples(index=False))
            new=[r for r in tables[key] if r.ts>cursor]
            for row in new:events.extend([(int(row.ts),1,key,row),(int(row.ts)+step,0,key,row)])
            if new:
                latest.append((key,len(tables[key])-1))
                if len(new)>1:event(s,'MISSED_SIGNAL_BARS',f'{key}|{cursor}|{new[-1].ts}',{'count':len(new)-1,'reason':'offline interval; never backfill candidate orders'})
        open_marks={}
        for at,kind,key,row in events:
            if kind==1:open_marks.setdefault(at,{})[key.split('|')[0]]=float(row.open)
        updated=set()
        for at,kind,key,row in sorted(events,key=lambda x:(x[0],x[1],x[2])):
            inst,bar=key.split('|')
            if kind==1:
                if at not in updated:
                    for symbol,px in open_marks[at].items():
                        if at>=s['marks'][symbol]['time']:s['marks'][symbol]=dict(price=px,time=at)
                    updated.add(at)
                for oid in list(s['pending']):
                    if s['pending'][oid]['symbol']==inst and s['pending'][oid]['bar']==bar:fill_pending(s,oid,row,at,snap,parent_completed)
            else:
                for oid in list(s['positions']):
                    if s['positions'][oid]['symbol']==inst and s['positions'][oid]['bar']==bar:position_bar(s,oid,row,int(row.ts),snap)
                if at>=s['marks'][inst]['time']:s['marks'][inst]=dict(price=float(row.close),time=at)
                s['watermarks'][key]=int(row.ts);s['cursor_hashes'][key]=digest(snap['frames'][key][tables[key].index(row)])
                event(s,'BAR_PROCESSED',f'{key}|{row.ts}',dict(close_time=iso(at)))
                observe(s,iso(at))
        # CURRENT marks only enter current decisions, never prior scheduled fills.
        s['marks']=deepcopy(snap['marks']);risk,u=observe(s,snap['observed_at_utc'])
        candidates=[]
        for key,i in latest:
            inst,bar=key.split('|');records=tables[key];prior=records[i]
            for core,strategy_id in IDS.items():
                side,score=signal(records,i,core)
                if side:candidates.append((-score,-float(prior.liquidity),inst,bar,core,side,prior))
        for _,_,inst,bar,core,side,prior in sorted(candidates,key=lambda x:x[:6]):
            cid=digest(dict(study=s['study_id'],strategy=core,symbol=inst,bar=bar,signal_close=int(prior.available)))
            if cid in s['candidates']:continue
            reasons=qualification_reasons(freeze,snap,snap['observed_at_utc'],inst)
            if day(snap['observed_at_utc']) in s['daily_halts']:reasons.append('DAILY_HALT_LATCHED')
            if any(p['symbol']==inst for p in list(s['pending'].values())+list(s['positions'].values())):reasons.append('SYMBOL_ALREADY_RESERVED')
            q=strategy_plan(core,prior,float(prior.close)*(1+side*.0005),side)
            plan_cost=None;allocated=None
            if q is None:reasons.append('STRUCTURE_INVALID')
            else:
                try:q=quantize_plan(q,s['instruments'][inst]['tickSz']);plan_cost=costs(q)
                except ValueError:reasons.append('TICK_INVALID')
                if plan_cost and plan_cost['net_rr']<3:reasons.append('NET_RR_BELOW_3')
            if prior.liquidity<CONFIG.min_quote_volume:reasons.append('LIQUIDITY')
            if q and plan_cost and plan_cost['net_rr']>=3:
                budget=min(1000.,max(0,2000-sum(e['risk_twd'] for e in exposures(s))))
                if budget<=0:reasons.append('TOTAL_RISK_CAP')
                else:
                    allocated=size(q,plan_cost['risk_per_unit'],32.,freeze['freeze_timestamp_utc'],'FROZEN_PAPER_FX',s['instruments'][inst],budget,5)
                    if not allocated:reasons.append('MINIMUM_SIZE')
            if allocated:
                gate=observe(s,snap['observed_at_utc'])[0].gate(core,allocated['risk_twd'],snap['observed_at_utc'])
                if gate!='ALLOW_RESEARCH':reasons.append(gate)
                gross=sum(e['notional_twd'] for e in exposures(s))
                if gross+allocated['notional_twd']>max(0,s['cash_twd']+u)*.45*5:reasons.append('MARGIN_BUDGET')
                if allocated['notional_twd']/32>prior.liquidity*.0001:reasons.append('PARTICIPATION')
            c=dict(id=cid,core=core,strategy_id=IDS[core],symbol=inst,bar=bar,side=side,signal_close_utc=iso(int(prior.available)),
                   observed_at_utc=snap['observed_at_utc'],state='REJECTED' if reasons else 'PENDING',reasons=sorted(set(reasons)),
                   planned_net_rr=plan_cost['net_rr'] if plan_cost else None,live_qualified=False,book='PAPER_FORWARD')
            s['candidates'][cid]=c;event(s,'CANDIDATE',cid,c)
            if not reasons and allocated:
                eligible=eligible_open(now,STEPS[bar])
                s['pending'][cid]=dict(**{k:v for k,v in c.items() if k not in ('state','reasons','book','live_qualified','planned_net_rr')},
                    strategy_version=freeze['strategy_version'],code_commit_sha=freeze['code_commit_sha'],freeze_timestamp_utc=freeze['freeze_timestamp_utc'],
                    execution=freeze['execution'],expected_entry=q.entry,stop=q.stop,tp1=q.tp1,tp2=q.tp2,
                    eligible_open_ts=eligible,expires_at=eligible+freeze['pending_ttl_bars']*STEPS[bar],
                    original_units=allocated['units'],remaining_units=allocated['units'],risk_twd=allocated['risk_twd'],notional_twd=allocated['notional_twd'],regime=prior.phase3_regime)
                event(s,'PENDING_CREATED',cid,{'eligible_open_utc':iso(eligible)})
        s['last_processed_at']=snap['observed_at_utc'];s['last_snapshot_hash']=digest(snap);s['last_snapshot_at']=snap['observed_at_utc']
        risk,u=observe(s,snap['observed_at_utc'])
        equity=s['cash_twd']+u
        s['equity'].append(dict(time=snap['observed_at_utc'],cash_twd=s['cash_twd'],unrealized_twd=u,equity_twd=equity,drawdown=1-equity/risk.peak))
        s['warnings']=['NEWS_UNKNOWN_NOT_NO_EVENT'] if snap['news']['status']!='VERIFIED_COVERAGE' else []
        s['warnings']+=['CROSS_NOT_VERIFIED','WIN_RATE_NOT_VERIFIED','OHLC_EXECUTION_UNCERTAIN']
    s['revision']+=1;s['last_success_at']=run_at;s['last_success_run_id']=str(run_id)
    event(s,'RUN_COMPLETE',str(run_id),dict(time=run_at,duplicate_input=duplicate))
    validate(s,freeze);return s,dict(duplicate_input=duplicate,previous_financial_hash=financial_hash(previous),financial_hash=financial_hash(s))
