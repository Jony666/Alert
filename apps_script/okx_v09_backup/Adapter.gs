/** Apps Script APIs only. No DriveApp, no deployment and no load-time I/O. */
function okxIO_() {
  var c=OKX_CONFIG, props=PropertiesService.getScriptProperties(), lock=LockService.getScriptLock();
  var io={now:function(){return Date.now();},sleep:function(ms){Utilities.sleep(ms);},
    bytes:function(s){return Utilities.newBlob(s).getBytes();},text:function(b){return Utilities.newBlob(b).getDataAsString('UTF-8');},
    sha:function(b){return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,b).map(function(n){return ('0'+((n+256)%256).toString(16)).slice(-2);}).join('');},
    unzip:function(b){var files={};Utilities.unzip(Utilities.newBlob(b,'application/zip')).forEach(function(blob){if(files[blob.getName()])throw new Error('ZIP_DUPLICATE');files[blob.getName()]=blob.getBytes();});return files;},
    fetch:function(url,opts){var r=UrlFetchApp.fetch(url,opts);return {status:r.getResponseCode(),headers:r.getAllHeaders(),bytes:r.getContent()};},
    githubHeaders:function(){var token=props.getProperty('OKX_V09_GITHUB_ACTIONS_READ_TOKEN');if(!token)throw new Error('GITHUB_READ_TOKEN_MISSING');return {Authorization:'Bearer '+token,Accept:'application/vnd.github+json'};},
    approved:function(){return props.getProperty('OKX_V09_UPLOAD_APPROVAL')==='I_APPROVE_ONE_APPS_SCRIPT_BACKUP';},
    grantedFolder:function(){return props.getProperty('OKX_V09_PICKER_GRANTED_FOLDER');},
    lock:function(){return lock.tryLock(1);},unlock:function(){lock.releaseLock();},
    lastAudit:function(){return props.getProperty('OKX_V09_LAST_AUDITED_KEY');},
    saveAudit:function(key){props.setProperty('OKX_V09_LAST_AUDITED_KEY',key);props.setProperty('OKX_V09_LAST_AUDIT_AT',String(Date.now()));}
  };
  var budget=new OkxBackup.Budget(c,io),api=OkxBackup.DRIVE;
  function call(url,options,retry){
    options=options||{};var googleToken;try{googleToken=ScriptApp.getOAuthToken();}catch(e){throw new Error('GOOGLE_REAUTHORIZATION_REQUIRED');}
    options.headers=Object.assign({},options.headers||{},{Authorization:'Bearer '+googleToken});
    return OkxBackup.http(io,budget,url,options,retry);
  }
  function json(r){return OkxBackup.jsonResponse(r,io);}
  function metadata(id,missing){
    if(!/^[A-Za-z0-9_-]+$/.test(String(id)))throw new Error('DRIVE_ID_INVALID');
    var r=call(api+'/files/'+id+'?fields=id,size,parents,trashed,sha256Checksum',{},true);
    if(missing&&r.status===404)return null;return json(r);
  }
  io.folderCheck=function(){
    var folder=json(call(api+'/files/'+c.folderId+'?fields=id,mimeType,trashed,driveId,capabilities(canAddChildren)',{},true));
    if(folder.id!==c.folderId||folder.mimeType!=='application/vnd.google-apps.folder'||folder.trashed||folder.driveId||!folder.capabilities.canAddChildren)throw new Error('PERSONAL_FOLDER_GRANT_NOT_WRITABLE');return folder;
  };
  io.preflight=function(required){
    io.folderCheck();var quota=json(call(api+'/about?fields=storageQuota(limit,usage)',{},true)).storageQuota;
    if(!quota||!/^\d+$/.test(quota.limit||'')||!/^\d+$/.test(quota.usage||''))throw new Error('PERSONAL_STORAGE_QUOTA_UNKNOWN');
    if(Number(quota.limit)-Number(quota.usage)<required)throw new Error('PERSONAL_STORAGE_QUOTA_INSUFFICIENT');
  };
  io.driveList=function(kind,key){
    if(['intent','package','verified-index','audit'].indexOf(kind)<0)throw new Error('DRIVE_QUERY_INVALID');
    var q="'"+c.folderId+"' in parents and trashed = false and appProperties has { key='application' and value='"+OkxBackup.APP+"' } and appProperties has { key='kind' and value='"+kind+"' }";
    if(key){if(!/^[0-9a-f]{64}$/.test(key))throw new Error('DRIVE_QUERY_INVALID');q+=" and appProperties has { key='backupKey' and value='"+key+"' }";}
    var rows=[],page='',seen={};
    for(var n=0;n<c.maxPages;n++){
      var d=json(call(api+'/files?q='+encodeURIComponent(q)+'&spaces=drive&pageSize=100&fields=nextPageToken,files(id,appProperties)'+(page?'&pageToken='+encodeURIComponent(page):''),{},true));
      rows=rows.concat(d.files||[]);if(!d.nextPageToken)return rows;
      if(seen[d.nextPageToken])throw new Error('DRIVE_PAGINATION_STALLED');page=d.nextPageToken;seen[page]=true;
    }throw new Error('DRIVE_PAGINATION_LIMIT');
  };
  io.driveDownload=function(id){
    var m=metadata(id),size=Number(m.size);
    if(m.trashed||!m.parents||m.parents.indexOf(c.folderId)<0||!Number.isSafeInteger(size)||size<0||size>c.maxBytes)throw new Error('REMOTE_SIZE_OR_PARENT_INVALID');
    var r=call(api+'/files/'+id+'?alt=media',{},true);
    if(r.status!==200||r.bytes.length!==size||(m.sha256Checksum&&io.sha(r.bytes)!==m.sha256Checksum))throw new Error('REMOTE_DOWNLOAD_INTEGRITY_FAILED');return r.bytes;
  };
  io.driveExists=function(id){return !!metadata(id,true);};
  io.driveIds=function(count){var ids=json(call(api+'/files/generateIds?space=drive&type=files&count='+count,{},true)).ids;
    if(!ids||ids.length!==count||ids.some(function(id){return !/^[A-Za-z0-9_-]+$/.test(id);})||new Set(ids).size!==count)throw new Error('DRIVE_IDS_INVALID');return ids;};
  function sameFile(id,raw){if(io.sha(io.driveDownload(id))!==io.sha(raw))throw new Error('EXISTING_FILE_DIFFERENT_NO_OVERWRITE');}
  function sessionURL(value){if(!/^https:\/\/(www|content)\.googleapis\.com\/upload\/drive\/v3\/files\?[^#]*$/.test(value||''))throw new Error('UNTRUSTED_GOOGLE_UPLOAD_SESSION');return value;}
  function offset(r,total){if(r.status===200||r.status===201)return total;if(r.status!==308)throw new Error('UPLOAD_STATUS_FAILED');var range=r.headers.Range||r.headers.range;if(!range)return 0;var m=/^bytes=0-(\d+)$/.exec(range);if(!m||Number(m[1])+1>total)throw new Error('UPLOAD_RANGE_INVALID');return Number(m[1])+1;}
  io.driveCreate=function(id,name,raw,mime,kind,key){
    if(!raw.length||raw.length>c.maxBytes)throw new Error('UPLOAD_SIZE_INVALID');
    if(metadata(id,true)){sameFile(id,raw);return;}
    var meta={id:id,name:name,mimeType:mime,parents:[c.folderId],appProperties:{application:OkxBackup.APP,kind:kind,backupKey:key}};
    var r=call(OkxBackup.UPLOAD+'?uploadType=resumable',{method:'post',contentType:'application/json',payload:JSON.stringify(meta),headers:{'X-Upload-Content-Type':mime,'X-Upload-Content-Length':String(raw.length)}},true);
    if(r.status===409){sameFile(id,raw);return;}
    if(r.status!==200&&r.status!==201)throw new Error('UPLOAD_SESSION_CREATE_FAILED');
    var location=sessionURL(r.headers.Location||r.headers.location),pos=0,failures=0,total=raw.length;
    while(pos<total){
      budget.check(30000);var end=Math.min(pos+1024*1024,total);
      r=call(location,{method:'put',contentType:mime,payload:raw.slice(pos,end),headers:{'Content-Range':'bytes '+pos+'-'+(end-1)+'/'+total}},false);
      if([200,201,308].indexOf(r.status)<0){
        if([0,429,500,502,503,504].indexOf(r.status)<0)throw new Error('UPLOAD_INTERRUPTED_KEEP_INTENT');
        failures++;budget.check(30000);io.sleep(1000*Math.min(failures,3));
        r=call(location,{method:'put',payload:'',headers:{'Content-Range':'bytes */'+total}},true);
      }
      var next=offset(r,total);if(next<pos)throw new Error('UPLOAD_OFFSET_ROLLBACK');if(next===pos)failures++;else failures=0;
      if(failures>=3)throw new Error('UPLOAD_RETRIES_EXHAUSTED');pos=next;
    }
    sameFile(id,raw);
  };
  io.record=function(receipt){
    var raw=io.bytes(JSON.stringify(receipt)),id=io.driveIds(1)[0];
    io.driveCreate(id,'audit-'+new Date(io.now()).toISOString().replace(/:/g,'-')+'.json',raw,'application/json','audit',receipt.key);
    props.setProperty('OKX_V09_LAST_RECEIPT',JSON.stringify(receipt));if(receipt.status==='VERIFIED'||receipt.status==='ALREADY_VERIFIED')props.setProperty('OKX_V09_LAST_BACKUP_RECEIPT',JSON.stringify(receipt));console.log(JSON.stringify(receipt));
  };
  return io;
}
function okxRun_(audit){
  try{var engine=new OkxBackup.Engine(OKX_CONFIG,okxIO_());return audit?engine.audit():engine.run();}
  catch(e){var code=/^[A-Z][A-Z0-9_]{2,100}$/.test(e.message||'')?e.message:'UNEXPECTED_SAFE_STOP';
    var receipt={status:'FAILED',code:code,at:new Date().toISOString(),production_state_written:false};
    PropertiesService.getScriptProperties().setProperty('OKX_V09_LAST_FAILURE',JSON.stringify(receipt));console.log(JSON.stringify(receipt));throw new Error(code);}
}
function backupOnce(){return okxRun_(false);}
function dailyBackup(){
  var r=backupOnce(),p=PropertiesService.getScriptProperties(),last=Number(p.getProperty('OKX_V09_LAST_AUDIT_AT')||0);
  if(Date.now()-last>=7*86400000)verifyRestore();return r;
}
function verifyRestore(){return okxRun_(true);}
function checkBackupStatus(){var p=PropertiesService.getScriptProperties(),raw=p.getProperty('OKX_V09_LAST_BACKUP_RECEIPT');var r=raw?JSON.parse(raw):null;
  return {last_receipt:r,last_failure:JSON.parse(p.getProperty('OKX_V09_LAST_FAILURE')||'null'),status:!r?'NO_BACKUP_YET':Date.now()-Date.parse(r.checked_at)>36*3600000?'BACKUP_OVERDUE':'RECENT_RECEIPT'};}
function installDailyTrigger(){
  OkxBackup.checkConfig(OKX_CONFIG);var p=PropertiesService.getScriptProperties();
  if(p.getProperty('OKX_V09_DAILY_APPROVAL')!=='I_APPROVE_DAILY_APPS_SCRIPT_BACKUP'||p.getProperty('OKX_V09_UPLOAD_APPROVAL')!=='I_APPROVE_ONE_APPS_SCRIPT_BACKUP')throw new Error('DAILY_TRIGGER_NOT_APPROVED');
  var existing=ScriptApp.getProjectTriggers().filter(function(t){return t.getHandlerFunction()==='dailyBackup';});
  if(existing.length>1)throw new Error('DUPLICATE_TRIGGERS_REVIEW_REQUIRED');if(existing.length===1)return 'EXISTING_TRIGGER_NOT_CHANGED';
  ScriptApp.newTrigger('dailyBackup').timeBased().atHour(8).nearMinute(0).everyDays(1).inTimezone('Asia/Taipei').create();return 'TRIGGER_INSTALLED_NOT_EXACT_TIME';
}
