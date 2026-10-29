/** Private spreadsheet-bound dialog; no public web app deployment. */
function showFolderPicker(){
  if(!/^\d+$/.test(OKX_CONFIG.projectNumber)||OKX_CONFIG.pickerApiKey==='USER_SETUP_REQUIRED')throw new Error('PICKER_PROJECT_SETUP_REQUIRED');
  SpreadsheetApp.getUi().showModalDialog(HtmlService.createHtmlOutputFromFile('Picker').setWidth(700).setHeight(500),'選取已指定的 v0.9 備份資料夾');
}
function pickerSetup(){
  return {token:ScriptApp.getOAuthToken(),appId:OKX_CONFIG.projectNumber,apiKey:OKX_CONFIG.pickerApiKey,folderId:OKX_CONFIG.folderId};
}
function confirmPickedFolder(id){
  if(id!==OKX_CONFIG.folderId)throw new Error('PICKER_WRONG_FOLDER');
  okxIO_().folderCheck();PropertiesService.getScriptProperties().setProperty('OKX_V09_PICKER_GRANTED_FOLDER',id);
  return '已驗證指定資料夾可存取；未上傳、未安裝觸發器。';
}
