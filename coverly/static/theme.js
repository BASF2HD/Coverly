/* Appearance is a browser preference; archive settings and files are untouched. */
(()=>{
 const media=window.matchMedia('(prefers-color-scheme: light)');
 let choice='dark';try{choice=localStorage.getItem('coverly-theme')||'dark';}catch{}
 function apply(value,persist=false){
  choice=['light','dark','system'].includes(value)?value:'dark';
  const resolved=choice==='system'?(media.matches?'light':'dark'):choice;
  document.documentElement.dataset.theme=resolved;
  document.documentElement.dataset.themeChoice=choice;
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content',resolved==='light'?'#f4f5ef':'#131512');
  document.querySelectorAll('[data-theme-toggle]').forEach(el=>{const label='Switch to '+(resolved==='light'?'dark':'light')+' theme';el.setAttribute('aria-label',label);el.title=label;el.querySelector('use')?.setAttribute('href',resolved==='light'?'#i-moon':'#i-sun');});
  if(persist)try{localStorage.setItem('coverly-theme',choice);}catch{}
 }
 apply(choice);
 document.addEventListener('DOMContentLoaded',()=>apply(choice));
 document.addEventListener('click',event=>{if(event.target.closest('[data-theme-toggle]'))apply(document.documentElement.dataset.theme==='light'?'dark':'light',true);});
 media.addEventListener('change',()=>{if(choice==='system')apply(choice);});
})();
