from __future__ import annotations
import csv, io, sqlite3, sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import pandas as pd
HERE=Path(__file__).resolve().parent; ROOT=HERE.parent; DATA=HERE/'data'; DB=DATA/'liquisense.sqlite3'
sys.path.insert(0,str(HERE/'liquisense_core'))
from engine import build_forecast, build_cash_alerts, clean_receivables, clean_payables, clean_bank_transactions, data_quality_checks
from document_import import extract_document, DocumentImportError
from ripple_engine import sample_network, simulate_network, downstream_payment_ids, optimize_rescue_plan
app=FastAPI(title='LiquiSense API',version='1.0.0',description='Local cash-flow forecast and payment ripple API')
app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_credentials=False,allow_methods=['*'],allow_headers=['*'])
class Tx(BaseModel):
    date:date
    description:str=Field(min_length=1,max_length=180)
    direction:str=Field(pattern='^(inflow|outflow)$')
    amount:float=Field(gt=0)
class SettingsIn(BaseModel):
    opening_cash:float=Field(ge=0)
    reserve_threshold:float=Field(ge=0)
class BusinessProfileIn(BaseModel):
    business_name:str=Field(min_length=1,max_length=100)
class UserProfileIn(BaseModel):
    display_name:str=Field(min_length=1,max_length=80)
class ForecastWindowIn(BaseModel):
    start_date:date
    horizon_days:int=Field(ge=1,le=90)
class ScheduleIn(BaseModel):
    receivables:list[dict[str,Any]]
    payables:list[dict[str,Any]]
class DocumentImportConfirm(BaseModel):
    transactions:list[Tx]=Field(min_length=1,max_length=500)
def transaction_key(dc,description,direction,amount):
    return (str(dc).strip()[:10],str(description).strip().casefold(),str(direction).strip().lower(),round(float(amount),2))
def db():
    con=sqlite3.connect(DB); con.row_factory=sqlite3.Row; return con
def init_db():
    DATA.mkdir(parents=True,exist_ok=True)
    with db() as con:
        con.execute('CREATE TABLE IF NOT EXISTS transactions(id INTEGER PRIMARY KEY,date TEXT,description TEXT,direction TEXT,amount REAL)')
        con.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value REAL NOT NULL)')
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('opening_cash',50000),('reserve_threshold',40000)")
        con.execute('CREATE TABLE IF NOT EXISTS business_profile(id INTEGER PRIMARY KEY CHECK(id=1),business_name TEXT NOT NULL)')
        con.execute("INSERT OR IGNORE INTO business_profile(id,business_name) VALUES(1,'Aarav Traders Pvt Ltd')")
        con.execute('CREATE TABLE IF NOT EXISTS user_profile(id INTEGER PRIMARY KEY CHECK(id=1),display_name TEXT NOT NULL)')
        con.execute("INSERT OR IGNORE INTO user_profile(id,display_name) VALUES(1,'Aarif S.')")
        con.execute('CREATE TABLE IF NOT EXISTS forecast_preferences(id INTEGER PRIMARY KEY CHECK(id=1),start_date TEXT NOT NULL,horizon_days INTEGER NOT NULL)')
        con.execute('INSERT OR IGNORE INTO forecast_preferences(id,start_date,horizon_days) VALUES(1,?,30)',(date.today().isoformat(),))
        if con.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]==0:
            for r in pd.read_csv(DATA/'bank_transactions.csv').to_dict('records'):
                con.execute('INSERT INTO transactions(date,description,direction,amount) VALUES(?,?,?,?)',(str(r['date']),str(r['description']).strip(),str(r['direction']).lower(),float(r['amount'])))
        con.execute('DELETE FROM transactions WHERE id NOT IN (SELECT MIN(id) FROM transactions GROUP BY date,lower(trim(description)),lower(trim(direction)),round(amount,2))')
        con.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_no_exact_duplicates ON transactions(date,lower(trim(description)),lower(trim(direction)),round(amount,2))')
def read(name): return pd.read_csv(DATA/f'{name}.csv')
def get_settings():
    with db() as con:
        rows=con.execute('SELECT key,value FROM settings').fetchall()
        profile=con.execute('SELECT business_name FROM business_profile WHERE id=1').fetchone()
        user=con.execute('SELECT display_name FROM user_profile WHERE id=1').fetchone()
        window=con.execute('SELECT start_date,horizon_days FROM forecast_preferences WHERE id=1').fetchone()
    values={r['key']:float(r['value']) for r in rows}
    return {'opening_cash':values.get('opening_cash',50000),'reserve_threshold':values.get('reserve_threshold',40000),'business_name':profile['business_name'] if profile else 'Aarav Traders Pvt Ltd','user_name':user['display_name'] if user else 'Aarif S.','forecast_start':window['start_date'] if window else date.today().isoformat(),'horizon_days':int(window['horizon_days']) if window else 30}
def txs():
    with db() as con: rows=con.execute('SELECT id,date,description,direction,amount FROM transactions ORDER BY date DESC,id DESC').fetchall()
    return [dict(x) for x in rows]
def make_forecast(horizon_days=30,opening_cash=None,reserve_threshold=None,delay_days=0,sales_drop_pct=0,emergency_expense=0,rescue_action=None,start_date=None):
    settings=get_settings(); opening_cash=settings['opening_cash'] if opening_cash is None else opening_cash; reserve_threshold=settings['reserve_threshold'] if reserve_threshold is None else reserve_threshold
    today=start_date or date.today(); rec=clean_receivables(read('receivables')); pay=clean_payables(read('payables'))
    upcoming=[t for t in txs() if date.fromisoformat(t['date'])>today]
    def schedule_key(name, amount, due):
        parsed=pd.to_datetime(due,errors='coerce')
        if pd.isna(parsed) or pd.isna(amount): return None
        return (str(name or '').strip().casefold(),parsed.date().isoformat(),round(float(amount),2))
    receipt_keys={schedule_key(r.get('customer'),r.get('amount'),r.get('due_date')) for r in rec.to_dict('records')}
    bill_keys={schedule_key(r.get('vendor'),r.get('amount'),r.get('due_date')) for r in pay.to_dict('records')}
    for t in upcoming:
        key=schedule_key(t['description'],t['amount'],t['date'])
        if t['direction']=='inflow':
            if key in receipt_keys: continue
            rec.loc[len(rec)]={'customer':t['description'],'invoice_id':f"TX-{t['id']}",'amount':t['amount'],'due_date':t['date'],'status':'outstanding','avg_delay_days':0}
            receipt_keys.add(key)
        else:
            if key in bill_keys: continue
            pay.loc[len(pay)]={'vendor':t['description'],'bill_id':f"TX-{t['id']}",'amount':t['amount'],'due_date':t['date'],'category':'Transaction','flexible':False,'status':'unpaid'}
            bill_keys.add(key)
    if 'due_date' in rec: rec['due_date']=pd.to_datetime(rec['due_date'],errors='coerce').dt.date
    if 'due_date' in pay: pay['due_date']=pd.to_datetime(pay['due_date'],errors='coerce').dt.date
    extension_days=0; applied_note=None
    if rescue_action=='collect_earlier' and not rec.empty:
        eligible=rec[rec['due_date'].notna() & rec['amount'].notna() & (rec['due_date']>today)].copy()
        if not eligible.empty:
            idx=eligible['due_date'].idxmin(); rec.at[idx,'due_date']=max(today,rec.at[idx,'due_date']-timedelta(days=7)); rec.at[idx,'avg_delay_days']=0; applied_note='Moved the next expected receipt up by to 7 days in this forecast.'
    elif rescue_action=='reschedule_supplier' and not pay.empty:
        eligible=pay[pay['due_date'].notna() & pay['amount'].notna() & ~pay['category'].astype(str).str.lower().isin({'rent','loan'})].copy()
        if not eligible.empty:
            idx=eligible['due_date'].idxmin(); pay.at[idx,'flexible']=True; extension_days=7; applied_note='Modeled a 7-day extension for the next eligible supplier bill; this requires supplier agreement.'
    elif rescue_action=='reduce_discretionary' and not pay.empty:
        eligible=~pay['category'].astype(str).str.lower().isin({'rent','loan'})
        pay.loc[eligible,'amount']=pay.loc[eligible,'amount']*0.8; applied_note='Reduced eligible non-rent and non-loan bills by 20% for this forecast.'
    result=build_forecast(float(opening_cash),rec,pay,today,int(horizon_days),float(reserve_threshold),int(delay_days),float(sales_drop_pct),float(emergency_expense),7,extension_days)
    bank=clean_bank_transactions(pd.DataFrame(txs()).drop(columns=['id'],errors='ignore')); alerts=build_cash_alerts(result.daily,float(reserve_threshold)*1.25,float(reserve_threshold))
    events=[]
    for _,r in result.receivables.iterrows():
        if r.expected_date<=today+timedelta(days=int(horizon_days)): events.append({'date':r.expected_date.isoformat(),'description':str(r.customer),'detail':str(r.invoice_id),'type':'inflow','amount':float(r.forecast_amount)})
    for _,r in result.payables.iterrows():
        if r.forecast_date<=today+timedelta(days=int(horizon_days)): events.append({'date':r.forecast_date.isoformat(),'description':str(r.vendor),'detail':str(r.bill_id),'type':'outflow','amount':float(r.amount)})
    events.sort(key=lambda x:x['date'])
    return {'as_of':today.isoformat(),'currency':'INR','opening_cash':opening_cash,'horizon_days':horizon_days,'minimum_balance':result.minimum_balance,'minimum_balance_date':result.minimum_balance_date.isoformat(),'ending_balance':result.ending_balance,'reserve_breach_date':result.reserve_breach_date.isoformat() if result.reserve_breach_date else None,'negative_balance_days':result.negative_balance_days,'expected_inflows':float(result.receivables.forecast_amount.sum()) if not result.receivables.empty else 0,'expected_outflows':float(result.payables.amount.sum()) if not result.payables.empty else 0,'points':[{'date':r.date.isoformat(),'balance':float(r.projected_balance),'inflows':float(r.inflows),'outflows':float(r.outflows)} for r in result.daily.itertuples()],'events':events,'alerts':[{'type':r.alert_type,'severity':r.severity,'date':r.date.isoformat(),'balance':float(r.projected_balance),'message':r.message} for r in alerts.itertuples()],'data_quality':data_quality_checks(bank,rec,pay,today),'rescue_action':rescue_action,'scenario_note':applied_note}
@app.on_event('startup')
def startup(): init_db()
@app.get('/')
def home(): return FileResponse(ROOT/'index.html')
@app.get('/api/health')
def health(): return {'status':'ok','name':'LiquiSense','storage':'local SQLite','currency':'INR'}
@app.get('/api/dashboard')
def dashboard(): return {'forecast':make_forecast(),'transactions':txs()[:10]}
@app.get('/api/settings')
def settings(): return get_settings()
@app.put('/api/settings')
def update_settings(x:SettingsIn):
    with db() as con:
        con.execute("INSERT INTO settings(key,value) VALUES('opening_cash',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(x.opening_cash,))
        con.execute("INSERT INTO settings(key,value) VALUES('reserve_threshold',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(x.reserve_threshold,))
    return get_settings()
@app.put('/api/forecast-window')
def update_forecast_window(x:ForecastWindowIn):
    with db() as con: con.execute('INSERT INTO forecast_preferences(id,start_date,horizon_days) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET start_date=excluded.start_date,horizon_days=excluded.horizon_days',(x.start_date.isoformat(),x.horizon_days))
    return {'forecast_start':x.start_date.isoformat(),'horizon_days':x.horizon_days}
@app.put('/api/user-profile')
def update_user_profile(x:UserProfileIn):
    name=x.display_name.strip()
    if not name: raise HTTPException(400,'Display name cannot be empty.')
    with db() as con: con.execute('INSERT INTO user_profile(id,display_name) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name',(name,))
    return {'display_name':name}
@app.put('/api/business-profile')
def update_business_profile(x:BusinessProfileIn):
    name=x.business_name.strip()
    if not name: raise HTTPException(400,'Business name cannot be empty.')
    with db() as con: con.execute('INSERT INTO business_profile(id,business_name) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET business_name=excluded.business_name',(name,))
    return {'business_name':name}
@app.get('/api/forecast-items')
def get_forecast_items():
    def clean(df, date_col):
        rows=df.to_dict('records')
        for r in rows:
            for key,value in list(r.items()):
                if pd.isna(value): r[key]=None
                elif hasattr(value,'item'): r[key]=value.item()
            if r.get(date_col) is not None: r[date_col]=str(r[date_col])[:10]
        return rows
    return {'receivables':clean(read('receivables'),'due_date'),'payables':clean(read('payables'),'due_date')}
@app.put('/api/forecast-items')
def update_forecast_items(x:ScheduleIn):
    rec=pd.DataFrame(x.receivables); pay=pd.DataFrame(x.payables)
    if not rec.empty:
        rec=clean_receivables(rec)
        if rec['customer'].eq('').any() or rec['amount'].isna().any() or (rec['amount']<=0).any() or rec['due_date'].isna().any(): raise HTTPException(400,'Each receipt needs a name, positive amount, and valid due date.')
    else: rec=pd.DataFrame(columns=['customer','invoice_id','amount','due_date','status','avg_delay_days'])
    if not pay.empty:
        pay=clean_payables(pay)
        if pay['vendor'].eq('').any() or pay['amount'].isna().any() or (pay['amount']<=0).any() or pay['due_date'].isna().any(): raise HTTPException(400,'Each bill needs a name, positive amount, and valid due date.')
    else: pay=pd.DataFrame(columns=['vendor','bill_id','amount','due_date','category','flexible','status'])
    def reject_duplicates(frame, name_column, label):
        seen=set()
        for row in frame.to_dict('records'):
            key=(str(row[name_column]).strip().casefold(),row['due_date'].isoformat(),round(float(row['amount']),2))
            if key in seen: raise HTTPException(409,f'Duplicate {label} found for the same name, due date, and amount. Remove or edit the duplicate before saving.')
            seen.add(key)
    reject_duplicates(rec,'customer','receipt')
    reject_duplicates(pay,'vendor','bill')
    if not rec.empty: rec['due_date']=rec['due_date'].map(lambda d:d.isoformat())
    if not pay.empty: pay['due_date']=pay['due_date'].map(lambda d:d.isoformat())
    rec.to_csv(DATA/'receivables.csv',index=False)
    pay.to_csv(DATA/'payables.csv',index=False)
    return {'saved':True,'receivables':len(rec),'payables':len(pay)}
@app.get('/api/transactions')
def get_transactions(): return txs()
@app.post('/api/transactions')
def add_transaction(x:Tx):
    name=x.description.strip(); direction=x.direction.strip().lower(); amount=round(float(x.amount),2)
    with db() as con:
        existing=con.execute('SELECT id FROM transactions WHERE date=? AND lower(trim(description))=lower(trim(?)) AND lower(trim(direction))=lower(trim(?)) AND round(amount,2)=?',(x.date.isoformat(),name,direction,amount)).fetchone()
        if existing: raise HTTPException(409,'This exact transaction is already saved; duplicate was not added.')
        con.execute('INSERT INTO transactions(date,description,direction,amount) VALUES(?,?,?,?)',(x.date.isoformat(),name,direction,amount))
    return {'saved':True,'duplicate':False}
@app.post('/api/import-csv')
async def import_csv(request:Request):
    try:
        rd=csv.DictReader(io.StringIO((await request.body()).decode('utf-8-sig'))); h={x.lower().strip():x for x in (rd.fieldnames or [])}
        dc=h.get('date'); desc=h.get('description') or h.get('merchant'); direction=h.get('direction') or h.get('type'); amount=h.get('amount')
        if not all((dc,direction,amount)): raise ValueError('CSV needs date, description, direction (inflow/outflow), and amount columns.')
        rows=[]
        for r in rd:
            kind=str(r[direction]).strip().lower()
            if kind not in {'inflow','income','receipt','in','outflow','expense','payment','out'}: raise ValueError(f'Invalid direction: {kind}. Use inflow or outflow.')
            kind='inflow' if kind in {'inflow','income','receipt','in'} else 'outflow'
            row_date=date.fromisoformat(r[dc].strip()).isoformat(); description=(r.get(desc,'Transaction') if desc else 'Transaction').strip() or 'Transaction'; value=abs(float(r[amount].replace('₹','').replace(',','').strip()))
            if value<=0: raise ValueError('Transaction amounts must be greater than zero.')
            rows.append((row_date,description,kind,round(value,2)))
        if not rows: raise ValueError('CSV has no transaction rows.')
        imported=0; skipped=0; seen=set()
        with db() as con:
            existing=con.execute('SELECT date,description,direction,amount FROM transactions').fetchall()
            seen.update(transaction_key(r['date'],r['description'],r['direction'],r['amount']) for r in existing)
            for row in rows:
                key=transaction_key(*row)
                if key in seen:
                    skipped+=1;continue
                con.execute('INSERT INTO transactions(date,description,direction,amount) VALUES(?,?,?,?)',row)
                seen.add(key);imported+=1
        return {'imported':imported,'skipped_duplicates':skipped,'total_rows':len(rows)}
    except (ValueError,TypeError,KeyError) as e: raise HTTPException(400,str(e)) from e
@app.post('/api/import-document')
async def import_document(request:Request,filename:str):
    if Path(filename).suffix.lower()=='.csv': raise HTTPException(400,'Use the CSV importer for .csv files.')
    try:
        raw=await request.body()
        return extract_document(filename,raw)
    except DocumentImportError as e:
        raise HTTPException(400,str(e)) from e
@app.post('/api/import-document/confirm')
def confirm_document_import(x:DocumentImportConfirm):
    imported=0; skipped=0; seen=set()
    with db() as con:
        existing=con.execute('SELECT date,description,direction,amount FROM transactions').fetchall()
        seen.update(transaction_key(r['date'],r['description'],r['direction'],r['amount']) for r in existing)
        for item in x.transactions:
            row=(item.date.isoformat(),item.description.strip(),item.direction,round(float(item.amount),2))
            key=transaction_key(*row)
            if key in seen:
                skipped+=1
                continue
            con.execute('INSERT INTO transactions(date,description,direction,amount) VALUES(?,?,?,?)',row)
            seen.add(key); imported+=1
    return {'imported':imported,'skipped_duplicates':skipped,'total_selected':len(x.transactions)}
@app.get('/api/forecast')
def forecast(horizon_days:int=30,opening_cash:float|None=None,reserve_threshold:float|None=None,delay_days:int=0,sales_drop_pct:float=0,emergency_expense:float=0,rescue_action:str|None=None,start_date:date|None=None):
    if not 1<=horizon_days<=90 or opening_cash<0 or reserve_threshold<0: raise HTTPException(400,'Use 1–90 days and non-negative cash/reserve values.')
    return make_forecast(horizon_days,opening_cash,reserve_threshold,delay_days,sales_drop_pct,emergency_expense,rescue_action,start_date)
@app.get('/api/ripple')
def ripple(delay_days:int=7,shock_payment_id:str='PAY-01',horizon_days:int=30):
    today=date.today(); businesses,payments=sample_network(today); r=simulate_network(businesses,payments,today,horizon_days,shock_payment_id,delay_days)
    return {'businesses':r.businesses.fillna('').to_dict('records'),'payments':r.payments.fillna('').to_dict('records'),'downstream_payment_ids':sorted(downstream_payment_ids(businesses,payments,shock_payment_id))}
@app.get('/api/rescue')
def rescue(delay_days:int=7,shock_payment_id:str='PAY-01',horizon_days:int=30):
    today=date.today(); businesses,payments=sample_network(today); downstream=sorted(downstream_payment_ids(businesses,payments,shock_payment_id)); extension=downstream[0] if downstream else None; table,best,feasible=optimize_rescue_plan(businesses,payments,today,horizon_days,shock_payment_id,max(1,delay_days),extension_payment_id=extension)
    return {'recommendation_percent':best.get('early_pct') if feasible and best else None,'best_option':best,'options':table.fillna('').to_dict('records')}

@app.get('/backend-client.js')
def client_script(): return FileResponse(ROOT/'backend'/'backend-client.js',headers={'Cache-Control':'no-store, max-age=0'})

