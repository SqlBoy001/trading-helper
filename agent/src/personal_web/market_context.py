"""One-shot public-source market context; no paid provider or model calls.

Default collects only broad market pools/rankings. --include-holdings requires
user authorization to query their securities at Eastmoney. No quantities sent.
"""
import argparse
import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from src.decision_journal import DecisionJournal

TZ=ZoneInfo('Asia/Shanghai')
SCOPE='东方财富涨跌停专题池；AKShare源码说明不含ST及科创板，不能当作全市场，尤其不能据此排除科创板ETF风险。'


def numeric(value):
    if isinstance(value,bool):return None
    try:v=float(value)
    except (ValueError,TypeError):return None
    return v if math.isfinite(v) else None


def diff_rows(payload):
    if payload.get('rc')!=0 or not isinstance(payload.get('data'),dict):raise ValueError('provider failure/missing data')
    data=payload['data'];items=data.get('diff')
    if isinstance(items,dict):items=list(items.values())
    if not isinstance(items,list):raise ValueError('rows missing')
    return items,data.get('total')


def parse_quote(row,date):
    stamp=numeric(row.get('f124'));at=datetime.fromtimestamp(stamp,TZ) if stamp else None
    return dict(code=str(row.get('f12','')),name=row.get('f14'),price=numeric(row.get('f2')),change_pct=numeric(row.get('f3')),
                source_time=at.isoformat() if at else None,closing_date_verified=bool(at and at.date().isoformat()==date and at.hour>=15))


def compare(stock,board):
    if not stock or not board or not stock.get('closing_date_verified') or not board.get('closing_date_verified'):
        return dict(status='unknown',excess_pp=None)
    s,b=numeric(stock.get('change_pct')),numeric(board.get('change_pct'))
    if s is None or b is None:return dict(status='unknown',excess_pp=None)
    return dict(status='against_up' if s>0 and b<0 else 'against_down' if s<0 and b>0 else 'same_or_flat',excess_pp=round(s-b,4))


def validate_pool(payload,date):
    data=payload.get('data');items=data.get('pool') if isinstance(data,dict) else None
    if payload.get('rc')!=0 or not isinstance(items,list) or str(data.get('qdate'))!=date.replace('-','') or data.get('tc')!=len(items):
        raise ValueError('pool date/count mismatch; not a verified empty pool')
    if len({(r.get('m'),r.get('c')) for r in items})!=len(items):raise ValueError('duplicate pool rows')
    return dict(date=date,count=len(items),items=items,scope=SCOPE,status='provider_complete')


class Fetcher:
    def __init__(self,directory):
        self.directory=directory;directory.mkdir(parents=True,exist_ok=True,mode=0o700);self.sources=[];self.last=0
    def get(self,label,base,params):
        if len(self.sources)>=55:raise ValueError('request limit 55 reached')
        time.sleep(max(0,1.1-(time.monotonic()-self.last)));self.last=time.monotonic()
        url=base+'?'+urllib.parse.urlencode(params);source=dict(id=label,url=url,fetched_at=datetime.now(TZ).isoformat());self.sources.append(source)
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Referer':'https://quote.eastmoney.com/'})
            with urllib.request.urlopen(req,timeout=12) as r:data=json.load(r)
            (self.directory/(label+'.json')).write_text(json.dumps(data,ensure_ascii=False));source['status']='received';return data
        except Exception as exc:source.update(status='failed',error=type(exc).__name__);raise


def snapshot(root):
    journal=DecisionJournal(root/'decision_journal.sqlite3');events=[];after=0
    while True:
        page=journal.history('personal:gtht',after=after,limit=100);events+=page['events'];after=page['next_after']
        if after is None:break
    event=max((e for e in events if e['kind']=='snapshot'),key=lambda e:(e['occurred_at'],e['recorded_at']),default=None)
    if event is None:raise ValueError('no holdings snapshot')
    amendments=journal.get(event['id'])['corrections']
    if amendments:event=dict(event,data=amendments[-1]['data']['replacement'])
    return event


def collect(root,date,include_holdings=False):
    now=datetime.now(TZ)
    if date!=now.date().isoformat() or now.hour<15:raise ValueError('prototype supports current-day after-close only; no historical mixing')
    fetch=Fetcher(root/'market'/now.strftime('%Y%m%d-%H%M%S'))
    out=dict(date=date,generated_at=now.isoformat(),sources=fetch.sources,errors=[],holdings=[],sector_risks=[],holdings_included=include_holdings,
             coverage_notes=[SCOPE,'行业与概念按东方财富分类分别比较；宽泛地域/指数标签不代表业务关联。','逆板块按当日涨跌幅正负方向比较，超额为百分点差；不是买卖信号。','同板块跌停是关联线索，不证明会传导到持仓。','ETF指数及成分权重尚未核实，不能套用普通股票分类。'],
             suspension_status='待核查公司/交易所原公告；没有检索结果不代表没有停牌。',leader_status='未确认行业龙头；连板高度候选不等于行业龙头。')
    def attempt(label,fn):
        try:return fn()
        except Exception as exc:out['errors'].append(dict(component=label,error=str(exc)));return None
    for kind,path,sort in [('up','getTopicZTPool','fbt:asc'),('down','getTopicDTPool','fund:asc')]:
        out['limit_'+kind]=attempt('pool-'+kind,lambda path=path,sort=sort:validate_pool(fetch.get('pool-'+kind,'https://push2ex.eastmoney.com/'+path,dict(ut='7eea3edcaed734bea9cbfc24409ed989',dpt='wz.ztzt',Pageindex=0,pagesize=200,sort=sort,date=date.replace('-',''))),date))
    boards=[];total=None
    for pn in range(1,9):
        response=attempt('boards-'+str(pn),lambda:fetch.get('boards-'+str(pn),'https://push2.eastmoney.com/api/qt/clist/get',dict(pn=pn,pz=100,po=1,np=1,fltt=2,fid='f3',fs='m:90+t:2',fields='f12,f14,f3,f2,f104,f105,f124')))
        if response is None:break
        result=attempt('board-rows',lambda:diff_rows(response))
        if result is None:break
        part,total=result;boards+=part
        if isinstance(total,int) and len(boards)>=total:break
        if not part:break
    unique={r['f12']:r for r in boards if r.get('f12')}
    out['boards_complete']=total is not None and len(unique)==total
    out['boards']=[dict(parse_quote(r,date),up_count=r.get('f104'),down_count=r.get('f105')) for r in unique.values()]
    if include_holdings:
        saved=snapshot(root);out.update(snapshot_id=saved['id'],snapshot_at=saved['occurred_at']);positions=saved['data']['positions']
        secids=','.join(('1.' if p['symbol'].endswith('.SH') else '0.')+p['symbol'].split('.')[0] for p in positions)
        payload=attempt('quotes',lambda:fetch.get('quotes','https://push2.eastmoney.com/api/qt/ulist.np/get',dict(secids=secids,fields='f12,f13,f14,f2,f3,f124',fltt=2)))
        qrows=attempt('quote-rows',lambda:diff_rows(payload)) if payload else None
        quotes={str(r['f12']):parse_quote(r,date) for r in qrows[0]} if qrows else {}
        memberships={};industries={};codes=[p['symbol'].split('.')[0] for p in positions if not p['symbol'].startswith(('5','1'))]
        if out['limit_down']:codes += [r['c'] for r in out['limit_down']['items']]
        for code in dict.fromkeys(codes):
            secid=('1.' if code.startswith('6') else '0.')+code
            for spt,name,target in [(1,'industry',industries),(3,'membership',memberships)]:
                response=attempt(name+'-'+code,lambda:fetch.get(name+'-'+code,'https://push2.eastmoney.com/api/qt/slist/get',dict(secid=secid,spt=spt,pi=0,pz=100,fields='f12,f13,f14,f3,f2,f124',fltt=2,po=1)))
                parsed=attempt('rows-'+code,lambda:diff_rows(response)) if response else None
                if parsed:
                    part,total=parsed
                    if total!=len(part):out['errors'].append(dict(component=name+'-'+code,error='incomplete membership'));continue
                    target[code]=[parse_quote(r,date) for r in part if r.get('f13')==90]
        for p in positions:
            code=p['symbol'].split('.')[0];stock=quotes.get(code);industry=industries.get(code,[])
            comparisons=[dict(board=b,**compare(stock,b)) for b in industry]
            concepts=[dict(board=b,**compare(stock,b)) for b in memberships.get(code,[]) if b['code'] not in unique]
            out['holdings'].append(dict(symbol=p['symbol'],name=p.get('name'),quote=stock,industry=comparisons,concept_divergences=[b for b in concepts if b['status'] in ['against_up','against_down']],all_memberships=memberships.get(code,[]),mapping_status='unverified_etf' if code.startswith(('5','1')) else 'available' if industry else 'missing'))
        if out['limit_down']:
            for item in out['limit_down']['items']:
                code=item['c'];risk_boards={b['code'] for b in memberships.get(code,[])};primary={b['code'] for b in industries.get(code,[])};associations=[]
                for p in out['holdings']:
                    common=[b for b in p['all_memberships'] if b['code'] in risk_boards]
                    if common:associations.append(dict(symbol=p['symbol'],name=p['name'],same_primary_industry=bool(primary & {b['board']['code'] for b in p['industry']}),shared_boards=common))
                out['sector_risks'].append(dict(stock=item['n'],code=code,change_pct=item['zdp'],industry=industries.get(code,[]),associations=associations,leader_status='未验证为龙头'))
    if out['limit_up']:out['consecutive_board_candidates']=[dict(code=r['c'],name=r['n'],consecutive_boards=r.get('lbc'),reported_industry=r.get('hybk')) for r in out['limit_up']['items'] if isinstance(r.get('lbc'),int) and r['lbc']>=3]
    out['request_count']=len(fetch.sources)
    path=root/'market-context.json';temp=path.with_suffix('.tmp');temp.write_text(json.dumps(out,ensure_ascii=False,indent=2));temp.replace(path)
    return out


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--data-dir',type=Path,required=True);parser.add_argument('--date',default=datetime.now(TZ).date().isoformat());parser.add_argument('--include-holdings',action='store_true',help='Only after user authorizes disclosing securities codes to Eastmoney.');args=parser.parse_args()
    result=collect(args.data_dir,args.date,args.include_holdings)
    print(json.dumps(dict(date=result['date'],requests=result['request_count'],errors=result['errors'],up=result['limit_up']['count'] if result['limit_up'] else None,down=result['limit_down']['count'] if result['limit_down'] else None,boards=len(result['boards']),boards_complete=result['boards_complete'],holdings_included=result['holdings_included']),ensure_ascii=False))
