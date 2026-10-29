const status=document.querySelector('#status'),button=document.querySelector('#capture');
let target,tab;
async function findConnection(){
 const tabs=await chrome.tabs.query({url:'http://localhost:8000/*'});
 for(const source of tabs){
  const album=await chrome.tabs.sendMessage(source.id,{type:'coverly-resolve-album'}).catch(()=>null);
  if(album?.albumId){
   const connection={...album,sourceTab:source.id,expires:Date.now()+1800000};
   await chrome.storage.session.set({'coverly-metadata-target':connection});return connection;
  }
 }
 if(target?.expires>Date.now()&&tabs.some(t=>t.id===target.sourceTab))return target;
 return null;
}
(async()=>{
 target=(await chrome.storage.session.get('coverly-metadata-target'))['coverly-metadata-target'];
 [tab]=await chrome.tabs.query({active:true,currentWindow:true});
 target=await findConnection();
 if(target){
  document.querySelector('#target').textContent='Import into: '+target.title;
  button.disabled=!/^https?:/.test(tab?.url||'')||tab.url.startsWith('http://localhost:8000');
  if(button.disabled)status.textContent='Open a film or album reference page, then click the Coverly extension again.';
 }else{
  document.querySelector('#target').textContent='No album is connected yet.';
  status.textContent='Click Open Coverly, open the album you want to update, then return to this reference page. If the album is already open, reload Coverly once so the updated bridge can connect.';
 }
})().catch(e=>status.textContent=e.message);
document.querySelector('#open').onclick=async()=>{
 const tabs=await chrome.tabs.query({url:'http://localhost:8000/*'});
 if(tabs.length){await chrome.tabs.update(tabs[0].id,{active:true});if(tabs[0].windowId)await chrome.windows.update(tabs[0].windowId,{focused:true});}
 else await chrome.tabs.create({url:'http://localhost:8000/#library'});
};
button.onclick=async()=>{
 button.disabled=true;status.textContent='Reading the selected page…';
 try{
  if(!target||target.expires<Date.now())throw Error('The album connection expired. Open the album in Coverly again.');
  if(/(^|\.)(google|bing)\./.test(new URL(tab.url).hostname))throw Error('Open a film or album result first, then use this button.');
  const results=await chrome.scripting.executeScript({target:{tabId:tab.id},func:captureCoverlyMetadata});
  const page=results[0]?.result;if(!page?.html)throw Error('No readable page metadata found.');
  const reply=await chrome.tabs.sendMessage(target.sourceTab,{type:'coverly-page-metadata',album_id:target.albumId,title:target.title,page});
  if(!reply?.ok)throw Error(reply?.error||'Reload Coverly in its original tab, then try again.');
  status.textContent='Details returned to Coverly. Review them before saving.';
  await chrome.tabs.update(target.sourceTab,{active:true});
 }catch(e){status.textContent=e.message;button.disabled=false;}
};
