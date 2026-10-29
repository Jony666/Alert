// Node is only the offline test host. Production .gs files have no Node imports.
const {test}=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),crypto=require('node:crypto'),zlib=require('node:zlib');
const root='apps_script/okx_v09_backup/';const ctx=vm.createContext({console});
vm.runInContext(fs.readFileSync(root+'Config.gs','utf8')+fs.readFileSync(root+'Core.gs','utf8'),ctx);
const B=ctx.OkxBackup,cfg=JSON.parse(JSON.stringify(ctx.OKX_CONFIG));
const sha=b=>crypto.createHash('sha256').update(Buffer.from(b)).digest('hex');
const buf=s=>Buffer.from(s),json=b=>JSON.parse(Buffer.from(b).toString());
const fixture=process.env.APPS_SCRIPT_FIXTURE;
assert(fixture,'APPS_SCRIPT_FIXTURE required; no synthetic market substitute');
const packageRaw=fs.readFileSync(fixture+'/package.zip'),envelope=fs.readFileSync(fixture+'/export.json'),env=json(envelope);
function zip(files){let local=[],central=[],offset=0;for(const [name,raw] of Object.entries(files)){let n=buf(name),h=Buffer.alloc(30);h.writeUInt32LE(0x04034b50);h.writeUInt16LE(20,4);h.writeUInt32LE(raw.length,18);h.writeUInt32LE(raw.length,22);h.writeUInt16LE(n.length,26);local.push(h,n,raw);let d=Buffer.alloc(46);d.writeUInt32LE(0x02014b50);d.writeUInt16LE(20,6);d.writeUInt32LE(raw.length,20);d.writeUInt32LE(raw.length,24);d.writeUInt16LE(n.length,28);d.writeUInt32LE(offset,42);central.push(d,n);offset+=h.length+n.length+raw.length;}let directory=Buffer.concat(central),end=Buffer.alloc(22);end.writeUInt32LE(0x06054b50);end.writeUInt16LE(central.length/2,8);end.writeUInt16LE(central.length/2,10);end.writeUInt32LE(directory.length,12);end.writeUInt32LE(offset,16);return Buffer.concat([...local,directory,end]);}
function unzip(raw){raw=Buffer.from(raw);let e=raw.length-22;while(e>=0&&raw.readUInt32LE(e)!==0x06054b50)e--;let p=raw.readUInt32LE(e+16),count=raw.readUInt16LE(e+10),out={};for(let i=0;i<count;i++){let size=raw.readUInt32LE(p+20),nl=raw.readUInt16LE(p+28),extra=raw.readUInt16LE(p+30),comment=raw.readUInt16LE(p+32),loc=raw.readUInt32LE(p+42),name=raw.subarray(p+46,p+46+nl).toString(),start=loc+30+raw.readUInt16LE(loc+26)+raw.readUInt16LE(loc+28),data=raw.subarray(start,start+size);out[name]=raw.readUInt16LE(p+10)===8?zlib.inflateRawSync(data):data;p+=46+nl+extra+comment;}return out;}
const archive=zip({'export.json':envelope,'package.zip':packageRaw});
function configuration(){return {...cfg,enabled:true,handoffReviewed:true,exportWorkflowId:99,exportHead:'a'.repeat(40),exporterFingerprint:env.exporter_fingerprint,maxPages:20};}
function fake(){let c=configuration(),now=Date.parse('2026-10-29T16:00:00Z'),files=new Map(),next=0,records=[],calls=[],locked=false,approved=true,granted=true;
 let source=JSON.parse(JSON.stringify(env.source_context));source.run.updated_at='2026-10-29T09:40:00Z';
 let exp={run:{id:9900,workflow_id:99,head_branch:'master',head_sha:c.exportHead,path:'.github/workflows/'+c.exportWorkflow,status:'completed',conclusion:'success',run_attempt:1,updated_at:'2026-10-29T15:00:00Z'},artifact:{id:9911,name:'okx-v09-drive-export-9900-1',expired:false,size_in_bytes:archive.length,digest:'sha256:'+sha(archive),created_at:'2026-10-29T15:00:00Z',expires_at:'2027-01-27T15:00:00Z',workflow_run:{id:9900,head_sha:c.exportHead,head_branch:'master'}}};
 const io={now:()=>now,sleep:ms=>{now+=ms;},bytes:buf,text:b=>Buffer.from(b).toString(),sha,unzip,
 approved:()=>approved,grantedFolder:()=>granted?c.folderId:null,lock:()=>{if(locked)return false;locked=true;return true;},unlock:()=>{locked=false;},
 githubHeaders:()=>({Authorization:'Bearer GITHUB_SECRET_SENTINEL'}),
 fetch:(url,options)=>{calls.push({url,options});let body;if(url.includes('/workflows/'+c.sourceWorkflow+'/runs'))body={workflow_runs:[source.run]};else if(url.includes('/workflows/'+c.exportWorkflow+'/runs'))body={workflow_runs:[exp.run]};else if(url.includes('/runs/'+source.run.id+'/artifacts'))body={artifacts:[source.artifact]};else if(url.includes('/runs/9900/artifacts'))body={artifacts:[exp.artifact]};else if(url.includes('/actions/artifacts?'))body={artifacts:[source.artifact,exp.artifact]};else if(url.endsWith('/artifacts/9911/zip'))return {status:302,headers:{Location:'https://productionresultssa18.blob.core.windows.net/test?sig=SIGNED_SECRET_SENTINEL'},bytes:buf('')};else if(url.includes('blob.core.windows.net/'))return {status:200,headers:{},bytes:archive};else throw Error('unexpected request');return {status:200,headers:{},bytes:buf(JSON.stringify(body))};},
 preflight:()=>{},driveList:(kind,key)=>[...files.values()].filter(v=>v.kind===kind&&(!key||v.key===key)).map(v=>({id:v.id})),
 driveDownload:id=>{assert(files.has(id),'remote missing');return files.get(id).raw;},driveExists:id=>files.has(id),driveIds:n=>Array.from({length:n},()=>String(++next)),
 driveCreate:(id,name,raw,mime,kind,key)=>{if(files.has(id))assert.equal(sha(files.get(id).raw),sha(raw),'overwrite forbidden');else files.set(id,{id,raw:Buffer.from(raw),kind,key});},
 record:r=>records.push(r),lastAudit:()=>null,saveAudit:()=>{}};
 return {c,io,files,records,calls,source,exp,setNow:v=>{now=v;},deny:()=>{approved=false;},ungrant:()=>{granted=false;},hold:()=>{locked=true;}};
}
function verified(f){return B.verify(archive,f.exp,f.source,f.c,f.io);}
function run(f){return new B.Engine(f.c,f.io).run();}

test('default config refuses before any external access',()=>{let f=fake();f.c.enabled=false;assert.throws(()=>run(f),/DISABLED/);assert.equal(f.calls.length,0);});
test('unreviewed publisher pin rejected',()=>{let f=fake();f.c.exportHead='HEAD';assert.throws(()=>run(f),/NOT_REVIEWED/);});
test('explicit upload approval mandatory',()=>{let f=fake();f.deny();assert.throws(()=>run(f),/NOT_APPROVED/);assert.equal(f.calls.length,0);});
test('same-app picker grant mandatory',()=>{let f=fake();f.ungrant();assert.throws(()=>run(f),/PICKER_GRANT/);});
test('concurrent writer rejected',()=>{let f=fake();f.hold();assert.throws(()=>run(f),/ALREADY_RUNNING/);});
test('real 22-file source and Python seals pass transport verification',()=>{let f=fake(),v=verified(f);assert.equal(Object.keys(v.originalFiles).length,22);assert.equal(v.source.revision,20);assert.equal(v.state.closed.length,0);});
test('complete simulated upload and verified index',()=>{let f=fake(),r=run(f);assert.equal(r.status,'VERIFIED');assert.equal(f.io.driveList('package').length,1);assert.equal(f.io.driveList('verified-index').length,1);});
test('duplicate performs verification without second package',()=>{let f=fake();run(f);let before=[...f.files].map(([k,v])=>[k,sha(v.raw)]);assert.equal(run(f).status,'ALREADY_VERIFIED');assert.deepEqual([...f.files].map(([k,v])=>[k,sha(v.raw)]),before);});
test('restore audit downloads and checks all source bytes',()=>{let f=fake();run(f);assert.equal(new B.Engine(f.c,f.io).audit().status,'RESTORE_VERIFIED_OFFLINE_NO_LEDGER_IMPORT');});
test('interrupted index recovers completed immutable package',()=>{let f=fake(),create=f.io.driveCreate;f.io.driveCreate=(...a)=>{if(a[4]==='verified-index')throw Error('SIMULATED_FAILURE');return create(...a);};assert.throws(()=>run(f));assert.equal(f.io.driveList('package').length,1);assert.equal(f.io.driveList('verified-index').length,0);f.io.driveCreate=create;assert.equal(run(f).status,'VERIFIED');assert.equal(f.io.driveList('package').length,1);});
test('failed package leaves intent and never verified index',()=>{let f=fake(),create=f.io.driveCreate;f.io.driveCreate=(...a)=>{if(a[4]==='package')throw Error('SIMULATED_FAILURE');return create(...a);};assert.throws(()=>run(f));assert.equal(f.io.driveList('intent').length,1);assert.equal(f.io.driveList('verified-index').length,0);f.io.driveCreate=create;run(f);assert.equal(f.io.driveList('package').length,1);});
test('post-upload corruption never produces success index',()=>{let f=fake(),create=f.io.driveCreate;f.io.driveCreate=(...a)=>{create(...a);if(a[4]==='package')f.files.get(a[0]).raw[0]^=1;};assert.throws(()=>run(f),/SHA256/);assert.equal(f.io.driveList('verified-index').length,0);});
test('existing corrupted backup is not silently reuploaded',()=>{let f=fake();run(f);let id=f.io.driveList('package')[0].id;f.files.get(id).raw[0]^=1;assert.throws(()=>run(f),/SHA256/);assert.equal(f.io.driveList('package').length,1);});
test('expired GitHub token reports reauthorization without raw body',()=>{let f=fake();f.io.fetch=()=>({status:401,bytes:buf('SECRET_BODY')});assert.throws(()=>run(f),e=>e.message==='TOKEN_EXPIRED_OR_REVOKED_REAUTHORIZE');});
test('permission denied never expands scopes',()=>{let f=fake();f.io.fetch=()=>({status:403,bytes:buf('SECRET')});assert.throws(()=>run(f),/NO_SCOPE_ESCALATION/);});
test('transient network failure retries bounded',()=>{let f=fake(),fetch=f.io.fetch,n=0;f.io.fetch=(...args)=>{if(n++<2)throw Error('SECRET_TOKEN');return fetch(...args);};assert.equal(run(f).status,'VERIFIED');});
test('persistent network failure terminates',()=>{let f=fake(),count=0;f.io.fetch=()=>{count++;throw Error('SECRET');};assert.throws(()=>run(f),/RETRIES_EXHAUSTED/);assert.equal(count,3);});
test('GitHub token never forwarded to redirected blob host',()=>{let f=fake();run(f);let request=f.calls.find(c=>c.url.includes('blob.core.windows.net/'));assert.equal(request.options.headers,undefined);assert.equal(request.options.followRedirects,false);});
test('untrusted redirect host blocked before request',()=>{for(let url of ['https://evil.example/a','http://productionresultssa18.blob.core.windows.net/a','https://productionresultssa18.blob.core.windows.net@evil.example/a','https://productionresultssa18.blob.core.windows.net:443/a'])assert.throws(()=>B.trustedBlob(url),/UNTRUSTED/);});
test('expired short-lived link is not persisted or treated successful',()=>{let f=fake(),fetch=f.io.fetch;f.io.fetch=(u,o)=>u.includes('blob.core.windows.net/')?{status:403}:fetch(u,o);assert.throws(()=>run(f),/SHORT_LIVED/);assert.equal(f.files.size,0);});
test('latest artifact missing does not fall back',()=>{let f=fake();f.exp.artifact.name='old';assert.throws(()=>run(f),/MISSING_OR_AMBIGUOUS/);});
test('latest source expired refuses fallback',()=>{let f=fake();f.source.artifact.expired=true;assert.throws(()=>run(f),/EXPIRED/);});
test('source older than 36 hours rejected even if latest successful',()=>{let f=fake();f.setNow(Date.parse('2026-11-01T00:00:00Z'));assert.throws(()=>run(f),/SOURCE_STALE/);});
test('artifact size cap enforced before blob download',()=>{let f=fake();f.exp.artifact.size_in_bytes=f.c.maxBytes+1;assert.throws(()=>run(f),/BEFORE_DOWNLOAD/);assert(!f.calls.some(c=>c.url.includes('blob.core.windows.net/')));});
test('artifact checksum corruption rejected',()=>{let f=fake();f.exp.artifact.digest='sha256:'+'0'.repeat(64);assert.throws(()=>run(f),/SHA256_FAILED/);});
test('changed workflow SHA rejected',()=>{let f=fake();f.exp.run.head_sha='b'.repeat(40);assert.throws(()=>run(f),/PROVENANCE/);});
test('rerun requires independent review',()=>{let f=fake();f.exp.run.run_attempt=2;assert.throws(()=>run(f),/PROVENANCE/);});
test('failed production run cannot be selected',()=>{let f=fake();f.source.run.conclusion='failure';assert.throws(()=>run(f),/LATEST_SUCCESS_MISSING/);});
test('source changes during upload keeps evidence but no false verified index',()=>{let f=fake(),fetch=f.io.fetch,n=0;f.io.fetch=(u,o)=>{if(u.includes('/workflows/'+f.c.sourceWorkflow+'/runs')&&++n===3)f.source.run.head_sha='0'.repeat(40);return fetch(u,o);};assert.throws(()=>run(f));assert.equal(f.io.driveList('verified-index').length,0);});
test('budget limit refuses before any write',()=>{let f=fake(),fetch=f.io.fetch;f.io.fetch=(...a)=>{f.setNow(f.io.now()+100000);return fetch(...a);};assert.throws(()=>run(f),/BUDGET/);assert.equal(f.files.size,0);});
test('archive traversal rejected before unzip',()=>{let f=fake();assert.throws(()=>B.zipInventory(zip({'../bad':buf('x')}),f.c),/PATH/);});
test('archive case-colliding names rejected',()=>{let f=fake();assert.throws(()=>B.zipInventory(zip({'A':buf('x'),'a':buf('x')}),f.c),/DUPLICATE/);});
test('ZIP expansion limit checked from directory before inflation',()=>{let f=fake(),raw=zip({'safe':buf('x')});let end=raw.length-22,pos=raw.readUInt32LE(end+16);raw.writeUInt32LE(50*1024*1024,pos+24);assert.throws(()=>B.zipInventory(raw,f.c),/EXPANSION/);});
test('ZIP64 rejected',()=>{let f=fake(),raw=zip({'safe':buf('x')}),pos=raw.readUInt32LE(raw.length-6);raw.writeUInt32LE(0xffffffff,pos+24);assert.throws(()=>B.zipInventory(raw,f.c),/UNSUPPORTED/);});
test('encrypted ZIP rejected',()=>{let f=fake(),raw=zip({'safe':buf('x')}),pos=raw.readUInt32LE(raw.length-6);raw.writeUInt16LE(1,pos+8);assert.throws(()=>B.zipInventory(raw,f.c),/UNSUPPORTED/);});
test('package tamper rejected even if new outer checksum supplied',()=>{let f=fake(),bad=Buffer.from(packageRaw);bad[100]^=1;assert.throws(()=>B.verify(zip({'export.json':envelope,'package.zip':bad}),f.exp,f.source,f.c,f.io),/SHA256/);});
test('export not corresponding to current formal artifact rejected',()=>{let f=fake();f.source.artifact.id++;assert.throws(()=>verified(f),/NOT_LATEST/);});
test('ledger rollback and same-revision forks refused',()=>{let f=fake(),a=verified(f),b={...a,source:{...a.source,revision:19}};assert.throws(()=>B.continuity(a,b),/ROLLBACK/);b={...a,source:{...a.source,state_hash:'0'.repeat(64)}};assert.throws(()=>B.continuity(a,b),/FORK/);});
test('journal truncation and watermark rollback refused',()=>{let f=fake(),a=verified(f),state=JSON.parse(JSON.stringify(a.state));state.journal=state.journal.slice(1);let b={...a,state,source:{...a.source,revision:21}};assert.throws(()=>B.continuity(a,b),/HISTORY_FORK/);});
test('exporter full verification cannot be replaced by SHA alone',()=>{let f=fake(),e={...env,python_full_verification:false};assert.throws(()=>B.verify(zip({'export.json':buf(JSON.stringify(e)),'package.zip':packageRaw}),f.exp,f.source,f.c,f.io),/ATTESTATION/);});
test('secrets and signed URLs absent from persisted index and receipts',()=>{let f=fake();run(f);let text=JSON.stringify([...f.files.values()].filter(v=>v.kind!=='package').map(v=>v.raw.toString()))+JSON.stringify(f.records);assert(!text.includes('GITHUB_SECRET_SENTINEL'));assert(!text.includes('SIGNED_SECRET_SENTINEL'));});
test('Apps Script manifest lists exact Drive scope and separate non-Drive scopes',()=>{let scopes=JSON.parse(fs.readFileSync(root+'appsscript.json')).oauthScopes;assert(scopes.includes('https://www.googleapis.com/auth/drive.file'));assert(!scopes.includes('https://www.googleapis.com/auth/drive'));assert(!scopes.includes('https://www.googleapis.com/auth/drive.readonly'));});
test('production source has no DriveApp or Node dependency or load-time deployment',()=>{let s=fs.readFileSync(root+'Adapter.gs','utf8')+fs.readFileSync(root+'Picker.gs','utf8');assert(!/DriveApp\s*\./.test(s));assert(!/require\(|process\./.test(s));assert(!/deleteTrigger|\.deleteFile|\.trash\(/.test(s));});

test('storage namespace handoff must be reviewed before any network access',()=>{let f=fake();f.c.handoffReviewed=false;assert.throws(()=>run(f),/HANDOFF_NOT_REVIEWED/);assert.equal(f.calls.length,0);});
test('oversized signed URL rejected before UrlFetch',()=>{assert.throws(()=>B.trustedBlob('https://productionresultssa18.blob.core.windows.net/test?'+ 'x'.repeat(2001)),/TOO_LONG/);});
