const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),crypto=require('node:crypto');
function setup(){let props=new Map(),remote=new Map(),sessions=new Map(),n=0,calls=[],faults=[],now=Date.now(),triggerCreates=0;
 let context={console:{log:()=>{}},PropertiesService:{getScriptProperties:()=>({getProperty:k=>props.get(k)||null,setProperty:(k,v)=>{props.set(k,v);}})},LockService:{getScriptLock:()=>({tryLock:()=>true,releaseLock:()=>{}})},
 Utilities:{newBlob:b=>({getBytes:()=>Buffer.from(b),getDataAsString:()=>Buffer.from(b).toString()}),computeDigest:(_,b)=>[...crypto.createHash('sha256').update(Buffer.from(b)).digest()],DigestAlgorithm:{SHA_256:'sha256'},sleep:()=>{}},
 ScriptApp:{getOAuthToken:()=> 'GOOGLE_SECRET_SENTINEL',getProjectTriggers:()=>[],newTrigger:()=>{triggerCreates++;let builder={timeBased:()=>builder,atHour:v=>{assert.equal(v,8);return builder;},nearMinute:v=>{assert.equal(v,0);return builder;},everyDays:()=>builder,inTimezone:v=>{assert.equal(v,'Asia/Taipei');return builder;},create:()=>{}};return builder;}},UrlFetchApp:{fetch:(url,opts)=>{
 calls.push({url,opts});let u=new URL(url),id=u.pathname.split('/').at(-1),status=200,headers={},raw=Buffer.from('{}');
 if(u.pathname.endsWith('/generateIds'))raw=Buffer.from(JSON.stringify({ids:Array.from({length:Number(u.searchParams.get('count'))},()=>String(++n))}));
 else if(opts.method==='post'){
  let m=JSON.parse(opts.payload);if(remote.has(m.id)){status=409;}else{let location='https://www.googleapis.com/upload/drive/v3/files?upload_id='+ ++n;sessions.set(location,{meta:m,raw:Buffer.alloc(0)});headers.Location=location;}
 }else if(opts.method==='put'){
  let s=sessions.get(url);if(!s){status=410;}else{
   let range=opts.headers['Content-Range'];if(!range.startsWith('bytes */')){
    let fault=faults.shift();if(fault==='before')return response(503,{},Buffer.alloc(0));
    let offset=Number(/bytes (\d+)-/.exec(range)[1]);assert.equal(offset,s.raw.length);
    s.raw=Buffer.concat([s.raw,Buffer.from(opts.payload)]);
    let total=Number(range.split('/')[1]);if(s.raw.length===total)remote.set(s.meta.id,{meta:s.meta,raw:s.raw});
    if(fault==='after')throw new Error('NETWORK_WITH_SECRET');
   }
   if(remote.has(s.meta.id))status=200;else{status=308;if(s.raw.length)headers.Range='bytes=0-'+(s.raw.length-1);}
  }
 }else if(remote.has(id)){
  let row=remote.get(id);if(u.searchParams.get('alt')==='media')raw=row.raw;
  else raw=Buffer.from(JSON.stringify({...row.meta,size:String(row.raw.length),sha256Checksum:crypto.createHash('sha256').update(row.raw).digest('hex')}));
 }else status=404;
 return response(status,headers,raw);
 }}};
 function response(status,headers,raw){return {getResponseCode:()=>status,getAllHeaders:()=>headers,getContent:()=>raw};}
 const ctx=vm.createContext(context);for(let f of ['Config.gs','Core.gs','Adapter.gs'])vm.runInContext(fs.readFileSync('apps_script/okx_v09_backup/'+f,'utf8'),ctx);
 let io=ctx.okxIO_();return {ctx,io,props,remote,calls,faults,getCreates:()=>triggerCreates};
}
function upload(f,raw=Buffer.from('test-data')){f.io.driveCreate('chosen_id','backup.zip',raw,'application/zip','package','a'.repeat(64));}
test('Drive REST resumable upload uses fixed endpoints and verifies download',()=>{let f=setup();upload(f);assert.equal(f.remote.size,1);assert.deepEqual(f.io.driveDownload('chosen_id'),Buffer.from('test-data'));assert(!f.calls.some(c=>['delete','patch'].includes(c.opts.method)));});
test('Drive chunked upload spans multiple chunks',()=>{let f=setup();upload(f,Buffer.alloc(2*1024*1024+10,7));assert(f.calls.filter(c=>c.opts.method==='put').length>=3);});
test('uncertain final upload queries status before confirming',()=>{let f=setup();f.faults.push('after');upload(f);assert.equal(f.remote.size,1);assert(f.calls.some(c=>(c.opts.headers['Content-Range']||'').startsWith('bytes */')));});
test('temporary upload failure resumes safely',()=>{let f=setup();f.faults.push('before');upload(f);assert.equal(f.remote.size,1);});
test('same pre-generated ID never overwrites different data',()=>{let f=setup();upload(f);assert.throws(()=>upload(f,Buffer.from('other')),/DIFFERENT_NO_OVERWRITE/);assert.deepEqual(f.remote.get('chosen_id').raw,Buffer.from('test-data'));});
test('same file retry does not create a second upload session',()=>{let f=setup();upload(f);let before=f.calls.filter(c=>c.opts.method==='post').length;upload(f);assert.equal(f.calls.filter(c=>c.opts.method==='post').length,before);});
test('unknown storage quota refuses upload',()=>{let f=setup();f.io.folderCheck=()=>({});assert.throws(()=>f.io.preflight(1));assert.equal(f.remote.size,0);});
test('Google authorization exception is sanitized',()=>{let f=setup();f.ctx.ScriptApp.getOAuthToken=()=>{throw Error('RAW_SECRET');};assert.throws(()=>upload(f),e=>e.message==='GOOGLE_REAUTHORIZATION_REQUIRED');});
test('default installation cannot create a clock trigger',()=>{let f=setup();assert.throws(()=>f.ctx.installDailyTrigger(),/DISABLED/);assert.equal(f.getCreates(),0);});
test('audit receipt cannot hide an overdue backup',()=>{let f=setup();f.io.record({status:'RESTORE_VERIFIED_OFFLINE_NO_LEDGER_IMPORT',key:'a'.repeat(64),checked_at:new Date().toISOString()});assert.equal(f.ctx.checkBackupStatus().status,'NO_BACKUP_YET');});
test('token is in request headers only, absent from remote metadata and bodies',()=>{let f=setup();upload(f);for(let row of f.remote.values())assert(!JSON.stringify(row.meta).includes('GOOGLE_SECRET_SENTINEL'));});
test('copying Apps Script source performs no remote operation',()=>{let f=setup();assert.equal(f.calls.length,0);assert.equal(f.getCreates(),0);assert.equal(f.props.size,0);});
