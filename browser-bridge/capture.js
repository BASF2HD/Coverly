/* Runs only after the user clicks the extension's Use this page button. */
function captureCoverlyMetadata(){
 const root=document.querySelector('main,article')||document.body;
 const copy=root.cloneNode(true);
 copy.querySelectorAll('script,style,iframe,form,input,textarea,button,img,video,audio,nav,footer,header,[hidden]').forEach(el=>el.remove());
 // Structured public album credits, not hydration state, cookies or account data.
 const allowed=new Set(['@type','name','musicBy','composer','byArtist','datePublished','dateReleased','track','tracks','hasPart','itemListElement','item','position','duration']);
 const prune=value=>Array.isArray(value)?value.slice(0,100).map(prune):value&&typeof value==='object'?Object.fromEntries(Object.entries(value).filter(([k])=>allowed.has(k)).map(([k,v])=>[k,prune(v)])):typeof value==='string'?value.slice(0,1000):value;
 copy.querySelectorAll('*').forEach(el=>[...el.attributes].forEach(a=>{if(!['class','colspan','rowspan'].includes(a.name)&&!(a.name==='id'&&el.id==='tracklist'))el.removeAttribute(a.name);}));
 const albums=[];
 function collect(value){if(Array.isArray(value))value.forEach(collect);else if(value&&typeof value==='object'){if(['Movie','MusicAlbum'].includes(value['@type']))albums.push(prune(value));else if(value['@graph'])collect(value['@graph']);}}
 document.querySelectorAll('script[type="application/ld+json"]').forEach(el=>{try{collect(JSON.parse(el.textContent));}catch{}});
 const scriptData=albums.map(data=>'<script type="application/ld+json">'+JSON.stringify(data).replaceAll('<','\\u003c')+'</script>').join('');
 const escape=text=>String(text).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 // Spotify's visible track grid is not an HTML table. Preserve performer roles.
 const tracks=[...root.querySelectorAll('[data-testid="tracklist-row"],[role="row"]')].slice(0,100).map(row=>{
  const song=row.querySelector('a[href*="/track/"]');if(!song)return '';
  const names=[...row.querySelectorAll('a[href*="/artist/"]')].map(a=>a.textContent.trim()).filter(Boolean);
  const length=(row.innerText.match(/\b\d{1,2}:\d{2}\b/g)||[]).at(-1)||'';
  return '<tr><td>'+escape(song.textContent)+'</td><td>'+escape([...new Set(names)].join(', '))+'</td><td>'+escape(length)+'</td></tr>';
 }).filter(Boolean);
 const table=tracks.length?'<table><tr><th>Title</th><th>Performers</th><th>Length</th></tr>'+tracks.join('')+'</table>':'';
 const h=document.querySelector('h1');const heading=h?'<h1>'+escape(h.innerText)+'</h1>':'';
 return {source_url:location.origin+location.pathname,html:(heading+copy.innerHTML+scriptData+table).slice(0,480000)};
}
