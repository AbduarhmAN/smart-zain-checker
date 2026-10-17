'use strict';
document.addEventListener('DOMContentLoaded',async()=>{
 await campaignFonts();let format='feed',motion;
 const elements=new Map(),grid=document.getElementById('postersGrid');
 const blobOf=canvas=>new Promise((resolve,reject)=>canvas.toBlob(blob=>blob?resolve(blob):reject(new Error('تعذر إنشاء الصورة')),'image/png'));
 CampaignPosts.forEach(post=>{
  const item=document.createElement('article');item.className='poster-item';
  const canvas=document.createElement('canvas');canvas.setAttribute('aria-label',post.name+': '+post.title.join(' '));
  const meta=document.createElement('div');meta.className='post-meta';meta.innerHTML='<span>اليوم '+post.day+'</span><span>'+post.id.slice(0,2)+'</span>';
  const heading=document.createElement('h3');heading.textContent=post.name;
  const actions=document.createElement('div');actions.className='poster-buttons';
  const download=document.createElement('button');download.className='button button-dark';download.textContent='حفظ الصورة ↓';download.addEventListener('click',async()=>{try{await saveCampaignFile(await blobOf(canvas),'hadiyat-check-'+post.id+'-'+format+'.png');campaignToast('حُفظ تصميم '+post.name);}catch(error){campaignToast(error.message);}});
  const copy=document.createElement('button');copy.className='button button-outline';copy.textContent='نسخ نص البوست';copy.addEventListener('click',()=>copyCampaignText(captionFor(post),'نُسخ نص البوست. يمكنك لصقه مع الصورة عند النشر.'));
  actions.append(download,copy);
  const details=document.createElement('details');details.className='caption-details';const summary=document.createElement('summary');summary.textContent='نص النشر';const caption=document.createElement('p');caption.textContent=captionFor(post);details.append(summary,caption);
  item.append(canvas,meta,heading,actions,details);grid.append(item);elements.set(post.id,{canvas,caption});CampaignArt.poster(canvas,post,format);
 });
 const redraw=()=>CampaignPosts.forEach(post=>{const entry=elements.get(post.id);CampaignArt.poster(entry.canvas,post,format);entry.canvas.style.aspectRatio=format==='story'?'9/16':'4/5';entry.caption.textContent=captionFor(post);});
 document.getElementById('posterFormat').addEventListener('change',event=>{format=event.target.value;redraw();});
 document.getElementById('applyKeyword').addEventListener('click',()=>{const value=document.getElementById('keyword').value.trim();if(!value){campaignToast('اكتب كلمة العرض أولًا.');return;}Campaign.keyword=value;redraw();motion.draw();campaignToast('تحدّثت كلمة العرض في التصميمات والفيديو ونصوص النشر.');});
 document.getElementById('downloadPack').addEventListener('click',async event=>{const button=event.currentTarget;button.disabled=true;button.textContent='جارٍ تجهيز الصور…';try{const zip=new JSZip();for(const post of CampaignPosts)zip.file('posts/'+post.id+'-'+format+'.png',await blobOf(elements.get(post.id).canvas));zip.file('captions-ar.md',CampaignPosts.map(post=>'## اليوم '+post.day+' · '+post.name+'\n\n'+captionFor(post)).join('\n\n'));zip.file('offer.txt','أول تشيكتين مجانًا، وبعدهم تشيكة مجانية كل يوم لحد نهاية العرض. مدة العرض أسبوعين. كل تشيكة لشيت واحد لحد 3000 عميل. ابعت «'+Campaign.keyword+'» في ماسنجر. التنفيذ حسب دور الحجز.');await saveCampaignFile(await zip.generateAsync({type:'blob',compression:'STORE'}),'hadiyat-check-11-posts-'+format+'.zip');campaignToast('حُفظت 11 صورة ونصوص النشر في ملف واحد.');}catch(error){campaignToast(error.message);}finally{button.disabled=false;button.textContent='تحميل كل البوستات ZIP ↓';}});
 const play=document.getElementById('studioPlay'),seek=document.getElementById('filmSeek'),restart=document.getElementById('studioRestart'),exportButton=document.getElementById('exportFilm'),status=document.getElementById('exportStatus');
 const sync=()=>{play.textContent=motion.playing?'إيقاف مؤقت':'تشغيل الإعلان';};
 motion=new CampaignMotion(document.getElementById('studioFilm'),{onUpdate:time=>{seek.value=time;},onComplete:sync});
 play.addEventListener('click',()=>{motion.playing?motion.pause():motion.play();sync();});restart.addEventListener('click',()=>{motion.restart();sync();});seek.addEventListener('input',()=>{motion.pause();motion.seek(Number(seek.value));sync();});

 const videoSelect=document.getElementById('videoSelect'),allExport=document.getElementById('exportAllFilms'),readyFilm=document.getElementById('readyFilm');
 const videoMeta=()=>CampaignVideos.find(v=>v.id===videoSelect.value);
 const selectVideo=()=>{
  motion.setVideo(videoSelect.value);const video=videoMeta();
  document.getElementById('videoDescription').textContent=video?.description||'الفيديو الأصلي: الخدمة والعرض في 7 مشاهد.';
  readyFilm.href='exports/'+(video?.filename||'hadiyat-check-silent-35s')+'.mp4';
  document.getElementById('studioFilm').setAttribute('aria-label',video?.name||'معاينة الإعلان الأصلي');sync();
 };
 videoSelect.addEventListener('change',selectVideo);
 const exportControls=[play,restart,seek,exportButton,allExport,videoSelect,document.getElementById('applyKeyword')];
 const exportOne=async(video)=>{
  const name=video?.name||'الإعلان الأصلي';
  status.textContent='جارٍ حفظ '+name+'… اترك الصفحة ظاهرة.';
  const {blob,extension}=await motion.exportVideo(progress=>{status.textContent='جارٍ حفظ '+name+' · '+Math.round(progress*100)+'٪';});
  await saveCampaignFile(blob,(video?.filename||'hadiyat-check-silent-35s')+'.'+extension);
  status.textContent='حُفظ '+name+' بدقة 1080 × 1920 بصيغة '+extension.toUpperCase()+'.';
 };
 exportButton.addEventListener('click',async()=>{
  exportControls.forEach(el=>el.disabled=true);
  try{await exportOne(videoMeta());campaignToast('الفيديو جاهز، دون أي مسار صوت.');}
  catch(error){status.textContent=error.message;campaignToast(error.message);}
  finally{exportControls.forEach(el=>el.disabled=false);sync();}
 });
 allExport.addEventListener('click',async()=>{
  exportControls.forEach(el=>el.disabled=true);
  try{for(const video of CampaignVideos){videoSelect.value=video.id;selectVideo();await exportOne(video);}status.textContent='حُفظت الفيديوهات الأربعة. كل فيديو 35 ثانية دون صوت.';campaignToast('الفيديوهات الأربعة جاهزة.');}
  catch(error){status.textContent=error.message;campaignToast(error.message);}
  finally{exportControls.forEach(el=>el.disabled=false);sync();}
 });
 document.addEventListener('visibilitychange',()=>{if(document.hidden && !motion.recording){motion.pause();sync();}});
});
