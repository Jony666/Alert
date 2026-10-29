/** Portable core; no Node/browser APIs, no credentials, no I/O on load. */
var OkxBackup = (function () {
  'use strict';
  var API='https://api.github.com/repos/Jony666/Alert';
  var DRIVE='https://www.googleapis.com/drive/v3';
  var UPLOAD='https://www.googleapis.com/upload/drive/v3/files';
  var APP='okx-v09-apps-script-v1';
  function stop(code){throw new Error(code);}
  function need(ok,code){if(!ok)stop(code);}
  function hex(value,n){return typeof value==='string' && new RegExp('^[0-9a-f]{'+n+'}$').test(value);}
  function eq(a,b){return stable(a)===stable(b);}
  function stable(x){if(Array.isArray(x))return '['+x.map(stable).join(',')+']';if(x&&typeof x==='object')return '{'+Object.keys(x).sort().map(function(k){return JSON.stringify(k)+':'+stable(x[k]);}).join(',')+'}';return JSON.stringify(x);}
  function parse(raw,io){try{return JSON.parse(io.text(raw));}catch(e){stop('JSON_INVALID');}}
  function sameSet(a,b){return eq(a.slice().sort(),b.slice().sort());}
  function sha(io,b){return io.sha(b);}
  function bytes(s,io){return io.bytes(s);}
  function checkConfig(c){
    need(c.enabled===true,'DISABLED_NO_EXTERNAL_ACCESS');
    need(c.handoffReviewed===true,'STORAGE_NAMESPACE_HANDOFF_NOT_REVIEWED');
    need(c.repo==='Jony666/Alert' && c.folderId==='1OlXqvYUPTl7sK5Eds71X_3gcRUQv1G-q','DESTINATION_UNREVIEWED');
    need(hex(c.exportHead,40)&&hex(c.exporterFingerprint,64)&&Number.isSafeInteger(c.exportWorkflowId)&&c.exportWorkflowId>0,'EXPORT_DEPLOYMENT_NOT_REVIEWED');
    need(c.maxBytes>0&&c.maxBytes<=12*1024*1024&&c.maxExpandedBytes<=40*1024*1024&&c.deadlineMs<=240000,'LIMITS_UNREVIEWED');
  }
  function Budget(c,io){this.c=c;this.io=io;this.start=io.now();}
  Budget.prototype.check=function(reserve){need(this.io.now()-this.start+(reserve||0)<this.c.deadlineMs,'EXECUTION_BUDGET_EXHAUSTED');};
  function http(io,budget,url,options,retry){
    options=options||{};options.followRedirects=false;options.muteHttpExceptions=true;
    for(var n=0;n<(retry?3:1);n++){
      budget.check(30000);var r;
      try{r=io.fetch(url,options);}catch(e){r={status:0};}
      if([0,429,500,502,503,504].indexOf(r.status)<0)return r;
      if(!retry) return r;
      if(n<2){budget.check(30000+1000*Math.pow(2,n));io.sleep(1000*Math.pow(2,n));}
    }
    stop('NETWORK_RETRIES_EXHAUSTED');
  }
  function jsonResponse(r,io){
    if(r.status===401)stop('TOKEN_EXPIRED_OR_REVOKED_REAUTHORIZE');
    if(r.status===403)stop('ACCESS_OR_RATE_LIMIT_DENIED_NO_SCOPE_ESCALATION');
    need(r.status===200||r.status===201,'API_READ_OR_WRITE_FAILED');return parse(r.bytes,io);
  }
  function github(io,budget,path){
    need(path.indexOf('/actions/')===0,'GITHUB_GET_ONLY');
    return jsonResponse(http(io,budget,API+path,{headers:io.githubHeaders()},true),io);
  }
  function pages(io,budget,c,path,key){
    var rows=[];
    for(var p=1;p<=c.maxPages;p++){
      var data=github(io,budget,path+(path.indexOf('?')<0?'?':'&')+'per_page=100&page='+p)[key];
      need(Array.isArray(data),'GITHUB_SCHEMA_INVALID');rows=rows.concat(data);if(data.length<100)return rows;
    }stop('PAGINATION_LIMIT_NO_TRUNCATION');
  }
  function select(io,budget,c,workflow,id,head,branch,prefix){
    var runs=pages(io,budget,c,'/actions/workflows/'+workflow+'/runs?branch='+branch,'workflow_runs');
    runs=runs.filter(function(r){return r.status==='completed'&&r.conclusion==='success';});
    need(runs.length>0,'LATEST_SUCCESS_MISSING');runs.sort(function(a,b){return a.updated_at===b.updated_at?b.id-a.id:(a.updated_at<b.updated_at?1:-1);});
    var r=runs[0];need(r.run_attempt===1&&r.workflow_id===id&&r.head_sha===head&&r.head_branch===branch&&r.path==='.github/workflows/'+workflow,'WORKFLOW_PROVENANCE_UNTRUSTED');
    var expected=prefix+r.id+'-1',all=pages(io,budget,c,'/actions/runs/'+r.id+'/artifacts','artifacts');
    var matches=all.filter(function(a){return a.name===expected;});need(matches.length===1,'LATEST_ARTIFACT_MISSING_OR_AMBIGUOUS');
    var a=matches[0];need(!a.expired&&Date.parse(a.expires_at)>io.now(),'LATEST_ARTIFACT_EXPIRED_NO_FALLBACK');
    need(Number.isSafeInteger(a.size_in_bytes)&&a.size_in_bytes>0&&a.size_in_bytes<=c.maxBytes,'ARTIFACT_SIZE_REJECTED_BEFORE_DOWNLOAD');
    need(/^sha256:[0-9a-f]{64}$/.test(a.digest),'ARTIFACT_SHA256_MISSING');
    var inventory=pages(io,budget,c,'/actions/artifacts','artifacts').filter(function(x){return x.name.indexOf(prefix)===0;});
    inventory.sort(function(x,y){return x.created_at===y.created_at?y.id-x.id:(x.created_at<y.created_at?1:-1);});
    need(inventory.length&&inventory[0].id===a.id,'NEWER_PUBLISHED_STATE_REQUIRES_REVIEW');
    need(a.workflow_run.id===r.id&&a.workflow_run.head_sha===head&&a.workflow_run.head_branch===branch,'ARTIFACT_OWNER_UNTRUSTED');
    return {run:r,artifact:a};
  }
  function source(io,b,c){return select(io,b,c,c.sourceWorkflow,c.sourceWorkflowId,c.sourceHead,'master','forward-state-'+c.study+'-');}
  function exported(io,b,c){return select(io,b,c,c.exportWorkflow,c.exportWorkflowId,c.exportHead,c.exportBranch,'okx-v09-drive-export-');}
  function identity(s){return [s.run.id,s.artifact.id,s.artifact.digest].join(':');}
  function trustedBlob(url){
    need(typeof url==='string'&&url.length<=2000,'ARTIFACT_REDIRECT_URL_TOO_LONG');
    var m=/^https:\/\/([^\/?#]+)(\/[^#]*)$/.exec(url||'');
    need(m&&!/[@:]/.test(m[1])&&/^productionresultssa\d+\.blob\.core\.windows\.net$/.test(m[1]),'UNTRUSTED_ARTIFACT_REDIRECT');return url;
  }
  function downloadArtifact(io,b,c,s){
    var first=http(io,b,API+'/actions/artifacts/'+s.artifact.id+'/zip',{headers:io.githubHeaders()},true);
    need(first.status===302,'ARTIFACT_REDIRECT_UNAVAILABLE');
    var url=trustedBlob(first.headers.location||first.headers.Location);
    // Never forward GitHub Authorization to the storage host. Never persist signed URLs.
    var r=http(io,b,url,{method:'get'},false);
    if(r.status===403||r.status===404)stop('SHORT_LIVED_LINK_EXPIRED_RETRY_NEXT_RUN');
    need(r.status===200,'ARTIFACT_DOWNLOAD_FAILED');
    need(r.bytes.length<=c.maxBytes&&sha(io,r.bytes)===s.artifact.digest.slice(7),'ARTIFACT_DOWNLOAD_SHA256_FAILED');return r.bytes;
  }
  // Read ZIP central directory BEFORE Utilities.unzip (no ZIP64/encryption/bombs/path traversal).
  function zipInventory(raw,c){
    function u16(i){return (raw[i]&255)|((raw[i+1]&255)<<8);}
    function u32(i){return ((raw[i]&255)+((raw[i+1]&255)*256)+((raw[i+2]&255)*65536)+((raw[i+3]&255)*16777216));}
    need(raw.length<=c.maxBytes&&raw.length>=22,'ZIP_SIZE_REJECTED');
    var end=-1;
    for(var i=raw.length-22;i>=Math.max(0,raw.length-65557);i--)if(u32(i)===0x06054b50&&i+22+u16(i+20)===raw.length){end=i;break;}
    need(end>=0&&u16(end+4)===0&&u16(end+6)===0&&u16(end+8)===u16(end+10),'ZIP_MULTIDISK_OR_EOCD_INVALID');
    var count=u16(end+10),pos=u32(end+16),size=u32(end+12),sum=0,names=[];
    need(count>0&&count<=1000&&pos+size===end,'ZIP_DIRECTORY_INVALID');
    for(var k=0;k<count;k++){
      need(pos+46<=end&&u32(pos)===0x02014b50,'ZIP_DIRECTORY_INVALID');
      var flags=u16(pos+8),method=u16(pos+10),compressed=u32(pos+20),expanded=u32(pos+24),nl=u16(pos+28),extra=u16(pos+30),comment=u16(pos+32),name='';
      need(!(flags&1)&&(method===0||method===8)&&compressed!==0xffffffff&&expanded!==0xffffffff,'ZIP_UNSUPPORTED');
      need(pos+46+nl+extra+comment<=end,'ZIP_DIRECTORY_INVALID');
      for(var j=0;j<nl;j++)name+=String.fromCharCode(raw[pos+46+j]&255);
      need(/^[A-Za-z0-9_.\/-]+$/.test(name)&&name[0]!=='/'&&name.split('/').every(function(p){return p&&p!=='.'&&p!=='..';})&&names.indexOf(name.toLowerCase())<0,'ZIP_PATH_UNSAFE_OR_DUPLICATE');
      need(expanded<=c.maxExpandedBytes&&expanded/Math.max(1,compressed)<=1000,'ZIP_EXPANSION_REJECTED');
      names.push(name.toLowerCase());sum+=expanded;pos+=46+nl+extra+comment;
    }
    need(pos===end&&sum<=c.maxExpandedBytes,'ZIP_TOTAL_EXPANSION_REJECTED');return names;
  }
  function unzip(raw,c,io){var declared=zipInventory(raw,c),files=io.unzip(raw),actual=Object.keys(files);need(sameSet(declared,actual.map(function(n){return n.toLowerCase();})),'ZIP_UNZIP_FILE_SET_MISMATCH');return files;}
  function hashes(files,manifest,io,excluded){
    need(sameSet(Object.keys(files),Object.keys(manifest).concat(excluded||[])),'FILE_SET_MISMATCH');
    Object.keys(manifest).forEach(function(n){need(hex(manifest[n],64)&&files[n]&&sha(io,files[n])===manifest[n],'FILE_SHA256_MISMATCH');});
  }
  function verify(raw,exportSelection,selected,c,io){
    var outer=unzip(raw,c,io);need(sameSet(Object.keys(outer),['export.json','package.zip']),'EXPORT_FILE_SET_MISMATCH');
    var e=parse(outer['export.json'],io),s=e.source;
    need(e.format==='OKX_V09_APPS_SCRIPT_EXPORT_V1'&&e.schema===1&&e.python_full_verification===true&&e.python_full_restore_verified===true&&e.production_state_written===false,'PYTHON_VERIFICATION_ATTESTATION_MISSING');
    need(e.exporter_fingerprint===c.exporterFingerprint&&hex(e.backup_key,64),'EXPORTER_VERSION_UNTRUSTED');
    need(s.study_id===c.study&&s.config_hash===c.configHash&&s.model_commit===c.modelCommit&&Number.isSafeInteger(s.revision)&&s.revision>0,'STUDY_VERSION_UNTRUSTED');
    var context=e.source_context;
    need(String(context.run.id)===String(s.run_id)&&context.artifact.id===s.artifact_id&&context.artifact.digest==='sha256:'+s.artifact_sha256&&context.run.head_sha===c.sourceHead&&context.run.conclusion==='success','SOURCE_CONTEXT_MISMATCH');
    if(selected)need(String(selected.run.id)===String(s.run_id)&&selected.artifact.id===s.artifact_id&&selected.artifact.digest==='sha256:'+s.artifact_sha256,'EXPORT_IS_NOT_LATEST_FORMAL_SOURCE');
    need(outer['package.zip'].length===e.package_size&&sha(io,outer['package.zip'])===e.package_sha256,'PACKAGE_SHA256_MISMATCH');
    var p=unzip(outer['package.zip'],c,io),pm=parse(p['package-manifest.json'],io);hashes(p,pm.files,io,['package-manifest.json']);
    need(pm.backup_key===e.backup_key&&pm.exporter_code_fingerprint===c.exporterFingerprint&&eq(pm.source,s)&&pm.production_resume_authorized===false,'PACKAGE_SOURCE_MISMATCH');
    need(sha(io,p['backup/backup-manifest.json'])===e.backup_manifest_sha256&&pm.backup_manifest_sha256===e.backup_manifest_sha256,'BACKUP_SEAL_MISMATCH');
    var bm=parse(p['backup/backup-manifest.json'],io),back={};
    Object.keys(p).filter(function(n){return n.indexOf('backup/')===0;}).forEach(function(n){back[n.slice(7)]=p[n];});hashes(back,bm.files,io,['backup-manifest.json']);
    need(sha(io,back['original-artifact.zip'])===s.artifact_sha256,'ORIGINAL_ZIP_SHA256_MISMATCH');
    var original=unzip(back['original-artifact.zip'],c,io),inventory=parse(back['source-inventory.json'],io);hashes(original,inventory,io,[]);
    var state=parse(original['state.json'],io),rm=parse(original['run_manifest.json'],io);
    need(state.revision===s.revision&&state.study_id===s.study_id&&rm.state_hash===s.state_hash,'LEDGER_SOURCE_MISMATCH');
    var report={},reportManifest=parse(p['report/report-manifest.json'],io);
    Object.keys(p).filter(function(n){return n.indexOf('report/')===0;}).forEach(function(n){report[n.slice(7)]=p[n];});hashes(report,reportManifest.files,io,['report-manifest.json']);
    need(reportManifest.source_zip_sha256===s.artifact_sha256,'REPORT_SOURCE_MISMATCH');
    return {envelope:e,state:state,source:s,key:e.backup_key,raw:raw,originalFiles:original,
      exportRun:exportSelection.run.id,exportArtifact:exportSelection.artifact.id,exportDigest:exportSelection.artifact.digest};
  }
  function continuity(old,current){
    need(old.source.study_id===current.source.study_id&&old.source.config_hash===current.source.config_hash,'REMOTE_STUDY_MISMATCH');
    need(old.source.revision<=current.source.revision,'LEDGER_REVISION_ROLLBACK');
    if(old.source.revision===current.source.revision)need(eq(old.source,current.source),'LEDGER_REVISION_FORK');
    else{
      need(eq(old.state.journal,current.state.journal.slice(0,old.state.journal.length))&&eq(old.state.closed,current.state.closed.slice(0,old.state.closed.length)),'LEDGER_HISTORY_FORK');
      Object.keys(old.state.watermarks).forEach(function(k){need(current.state.watermarks[k]>=old.state.watermarks[k],'LEDGER_WATERMARK_ROLLBACK');});
    }
  }
  function Engine(c,io){this.c=c;this.io=io;this.budget=new Budget(c,io);}
  Engine.prototype.gate=function(){checkConfig(this.c);need(this.io.approved(),'UPLOAD_NOT_APPROVED');need(this.io.grantedFolder()===this.c.folderId,'SAME_APP_PICKER_GRANT_REQUIRED');};
  Engine.prototype.remote=function(row){
    var raw=this.io.driveDownload(row.file_id);need(sha(this.io,raw)===row.sha256,'REMOTE_ZIP_SHA256_FAILED');
    var result=verify(raw,{run:{id:row.export_run_id},artifact:{id:row.export_artifact_id,digest:'sha256:'+row.sha256}},null,this.c,this.io);
    need(row.key===result.key&&eq(row.source,result.source),'REMOTE_INDEX_SOURCE_MISMATCH');return result;
  };
  Engine.prototype.catalog=function(){
    var rows=this.io.driveList('verified-index'),seen={},revs={};
    return rows.map(function(meta){var d=parse(this.io.driveDownload(meta.id),this.io);need(d.schema===1&&d.status==='VERIFIED'&&hex(d.key,64)&&hex(d.sha256,64)&&!seen[d.key],'REMOTE_INDEX_INVALID');
      need(d.source&&Number.isSafeInteger(d.source.revision)&&d.source.revision>0,'REMOTE_INDEX_INVALID');
      if(revs[d.source.revision])need(eq(revs[d.source.revision],d.source),'REMOTE_INDEX_REVISION_FORK');revs[d.source.revision]=d.source;seen[d.key]=true;return d;},this);
  };
  Engine.prototype.run=function(){
    this.gate();need(this.io.lock(),'BACKUP_ALREADY_RUNNING');
    try{
      var io=this.io,c=this.c,b=this.budget,s=source(io,b,c),x=exported(io,b,c);
      need(io.now()-Date.parse(s.artifact.created_at)<=c.maxSourceAgeMs&&io.now()>=Date.parse(s.artifact.created_at),'SOURCE_STALE_OR_CLOCK_INVALID');
      var raw=downloadArtifact(io,b,c,x),verified=verify(raw,x,s,c,io);io.preflight(raw.length+65536);
      var catalog=this.catalog();catalog.sort(function(a,d){return d.source.revision-a.source.revision;});
      if(catalog.length)continuity(this.remote(catalog[0]),verified);
      var same=catalog.filter(function(d){return d.key===verified.key;});
      if(same.length){this.remote(same[0]);this.recheck(s);return this.receipt('ALREADY_VERIFIED',same[0]);}
      var recover=io.driveList('intent',verified.key).map(function(m){return parse(io.driveDownload(m.id),io);});
      var completed=recover.filter(function(d){return io.driveExists(d.file_id);});need(completed.length<=1,'AMBIGUOUS_INTENT_REQUIRES_REVIEW');
      var row;
      if(completed.length){row=completed[0];continuity(this.remote(row),verified);need(row.key===verified.key,'INTENT_SOURCE_MISMATCH');}
      else{
        b.check(60000);this.recheck(s);var ids=io.driveIds(3);
        row={schema:1,key:verified.key,source:verified.source,file_id:ids[1],index_id:ids[2],intent_id:ids[0],sha256:sha(io,raw),size:raw.length,
          export_run_id:x.run.id,export_artifact_id:x.artifact.id,source_expires_at:s.artifact.expires_at,export_expires_at:x.artifact.expires_at,created_at:new Date(io.now()).toISOString()};
        io.driveCreate(ids[0],'intent-'+verified.key+'.json',bytes(JSON.stringify(row),io),'application/json','intent',verified.key);
        io.driveCreate(ids[1],'okx-v09-r'+verified.source.revision+'-'+verified.key.slice(0,16)+'.zip',raw,'application/zip','package',verified.key);
        this.remote(row);
      }
      b.check(30000);this.recheck(s);row.status='VERIFIED';row.verified_at=new Date(io.now()).toISOString();
      io.driveCreate(row.index_id,'verified-'+verified.key+'.json',bytes(JSON.stringify(row),io),'application/json','verified-index',verified.key);
      var readback=parse(io.driveDownload(row.index_id),io);need(eq(row,readback),'INDEX_READBACK_FAILED');
      return this.receipt('VERIFIED',row);
    }finally{this.io.unlock();}
  };
  Engine.prototype.recheck=function(expected){need(identity(source(this.io,this.budget,this.c))===identity(expected),'SOURCE_ADVANCED_RETRY_NEXT_RUN');};
  Engine.prototype.receipt=function(status,row){
    var r={status:status,source:row.source,key:row.key,sha256:row.sha256,drive_file_id:row.file_id,export_run_id:row.export_run_id,export_artifact_id:row.export_artifact_id,
      source_expires_at:row.source_expires_at,export_expires_at:row.export_expires_at,expiry_warning:Date.parse(row.source_expires_at)-this.io.now()<30*86400000,
      checked_at:new Date(this.io.now()).toISOString(),original_zip_and_all_bundle_files_verified:true,production_state_written:false};
    this.io.record(r);return r;
  };
  Engine.prototype.audit=function(){
    this.gate();need(this.io.lock(),'BACKUP_ALREADY_RUNNING');
    try{var rows=this.catalog();need(rows.length,'NO_VERIFIED_BACKUP');
      var last=this.io.lastAudit();rows.sort(function(a,b){return a.source.revision-b.source.revision;});
      var target=rows.filter(function(r){return r.key!==last;})[0]||rows[0];this.budget.check(60000);this.remote(target);
      var receipt=this.receipt('RESTORE_VERIFIED_OFFLINE_NO_LEDGER_IMPORT',target);this.io.saveAudit(target.key);return receipt;
    }finally{this.io.unlock();}
  };
  return {Engine:Engine,checkConfig:checkConfig,Budget:Budget,zipInventory:zipInventory,verify:verify,continuity:continuity,trustedBlob:trustedBlob,select:select,downloadArtifact:downloadArtifact,sha:sha,APP:APP,DRIVE:DRIVE,UPLOAD:UPLOAD,http:http,jsonResponse:jsonResponse,stable:stable};
}());
