'use strict';
document.addEventListener('DOMContentLoaded',async()=>{
 document.querySelectorAll('.copy-keyword').forEach(button=>button.addEventListener('click',()=>copyCampaignText(Campaign.keyword)));
 await campaignFonts();
 const reduced=matchMedia('(prefers-reduced-motion: reduce)'),button=document.getElementById('filmPlay');
 const motion=new CampaignMotion(document.getElementById('promoFilm'),{loop:true,onUpdate:seconds=>{document.getElementById('filmProgress').style.transform='scaleX('+(seconds/CampaignArt.duration)+')';}});
 const updateButton=()=>{button.textContent=motion.playing?'Ⅱ':'▶';button.setAttribute('aria-label',motion.playing?'إيقاف الإعلان مؤقتًا':'تشغيل الإعلان الصامت');};
 button.addEventListener('click',()=>{motion.playing?motion.pause():motion.play();updateButton();});
 if(!reduced.matches){motion.play();gsap.from('.hero-copy > *',{opacity:0,y:22,duration:.7,stagger:.09,ease:'power3.out'});}updateButton();
 let visible=true,backgroundPaused=false;
 new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;if(!visible && motion.playing){motion.pause();backgroundPaused=true;updateButton();}else if(visible && backgroundPaused && !document.hidden && !reduced.matches){motion.play();backgroundPaused=false;updateButton();}},{threshold:.15}).observe(document.getElementById('promoFilm'));
 document.addEventListener('visibilitychange',()=>{if(document.hidden && motion.playing){motion.pause();backgroundPaused=true;}else if(!document.hidden && visible && backgroundPaused && !reduced.matches){motion.play();backgroundPaused=false;}updateButton();});
 reduced.addEventListener('change',()=>{if(reduced.matches){motion.pause();updateButton();}});
});
