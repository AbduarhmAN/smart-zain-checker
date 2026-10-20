'use strict';
window.CampaignArt=(()=>{
 const palettes={dark:{bg:'#123f35',ink:'#f4f6e9',muted:'#bed0bb',accent:'#d0ec89',line:'#42725c',paper:'#f4f6e9'},paper:{bg:'#f4f6e9',ink:'#173d31',muted:'#60765e',accent:'#b4d974',line:'#d4dfc8',paper:'#e7efdc'},lime:{bg:'#d0ec89',ink:'#173d31',muted:'#49643d',accent:'#173d31',line:'#a6c671',paper:'#f4f6e9'}};
 const arabic=n=>String(n).replace(/\d/g,d=>'٠١٢٣٤٥٦٧٨٩'[d]);
 function rect(c,x,y,w,h,r,fill,stroke){c.beginPath();c.roundRect(x,y,w,h,r);if(fill){c.fillStyle=fill;c.fill();}if(stroke){c.strokeStyle=stroke;c.lineWidth=2;c.stroke();}}
 function text(c,str,x,y,size,color,align='right',weight=700,max=940){c.fillStyle=color;c.textAlign=align;c.direction='rtl';while(size>18){c.font=weight+' '+size+'px Tajawal';if(c.measureText(str).width<=max)break;size-=2;}c.fillText(str,x,y);}
 function check(c,x,y,size,color){c.beginPath();c.moveTo(x,y+size*.48);c.lineTo(x+size*.32,y+size*.77);c.lineTo(x+size,y);c.strokeStyle=color;c.lineWidth=size*.13;c.lineCap='round';c.lineJoin='round';c.stroke();}
 function star(c,x,y,r,color){c.save();c.translate(x,y);c.fillStyle=color;for(let i=0;i<8;i++){c.rotate(Math.PI/4);rect(c,-r*.12,-r,r*.24,r*.75,r*.11,color);}c.restore();}
 function logo(c,p,w=1080){rect(c,w-145,66,75,75,18,p.accent);check(c,w-127,91,39,p.bg);text(c,'تشيك',w-162,115,53,p.ink);text(c,'وقتُك للمتابعة',w-163,147,24,p.muted,'right',400);}
 function sheet(c,x,y,w,h,p,rotation=0,marked=0){c.save();c.translate(x+w/2,y+h/2);c.rotate(rotation);x=-w/2;y=-h/2;rect(c,x,y,w,h,20,p.paper);rect(c,x+24,y+25,w-48,48,9,'#dde8cf');text(c,'رقم البحث',x+w-35,y+56,24,'#325a3e','right',700,w*.55);text(c,'المبلغ',x+48,y+56,22,'#325a3e','left',700,w*.32);const rows=5,span=(h-115)/rows;for(let i=0;i<rows;i++){const ry=y+100+i*span;c.strokeStyle='#cedbc3';c.lineWidth=1.5;c.beginPath();c.moveTo(x+24,ry+span-17);c.lineTo(x+w-24,ry+span-17);c.stroke();rect(c,x+w*.51,ry,w*.35,12,4,'#c3d1b6');rect(c,x+38,ry,w*.26,12,4,'#d3dfc6');if(i<marked)check(c,x+w-42,ry-5,16,'#46733e');}c.restore();}
 function calendar(c,x,y,w,h,p,progress=14){rect(c,x,y,w,h,24,p.paper);rect(c,x+20,y+20,w-40,65,12,'#dde8cf');text(c,'مدة العرض أسبوعين',x+w/2,y+62,34,'#254e37','center');const cw=(w-48)/7,ch=(h-120)/2;for(let i=0;i<14;i++){const cx=x+w-24-(i%7+1)*cw,cy=y+105+Math.floor(i/7)*ch;rect(c,cx+6,cy,cw-12,ch-16,12,i<progress?'#b7d882':'#e6eddc');text(c,arabic(i+1),cx+cw/2,cy+(ch-16)/2+11,32,'#254e37','center');}}
 function clock(c,x,y,r,p,t=0){c.beginPath();c.arc(x,y,r,0,Math.PI*2);c.strokeStyle=p.line;c.lineWidth=4;c.stroke();for(let i=0;i<12;i++){const a=i*Math.PI/6;c.beginPath();c.moveTo(x+Math.sin(a)*r*.86,y-Math.cos(a)*r*.86);c.lineTo(x+Math.sin(a)*r*.92,y-Math.cos(a)*r*.92);c.strokeStyle=p.muted;c.lineWidth=3;c.stroke();}c.beginPath();c.moveTo(x,y);c.lineTo(x+r*.5,y-r*.23);c.moveTo(x,y);c.lineTo(x+Math.sin(t)*r*.64,y-Math.cos(t)*r*.64);c.strokeStyle=p.ink;c.lineWidth=15;c.lineCap='round';c.stroke();c.beginPath();c.arc(x,y,12,0,Math.PI*2);c.fillStyle=p.accent;c.fill();}
 function illustration(c,kind,p,y,scale=1,progress=1){c.save();c.translate(540,y);c.scale(scale,scale);switch(kind){
  case 'welcome':sheet(c,-335,-100,355,350,p,-.09,5);sheet(c,-5,-50,355,350,p,.09,5);rect(c,-110,-130,220,140,20,p.accent);text(c,'٢',0,-25,130,p.bg,'center');text(c,'أول اتنين علينا',0,365,47,p.ink,'center');break;
  case 'capacity':sheet(c,-220,-100,440,385,p,-.06,Math.round(progress*5));rect(c,-320,50,640,180,22,p.bg);text(c,'٣٠٠٠',0,199,165,p.ink,'center');text(c,'عميل لكل شيت',0,333,48,p.ink,'center');break;
  case 'calendar':calendar(c,-370,-65,740,300,p,Math.round(progress*14));text(c,'واحدة كل يوم',0,330,57,p.ink,'center');break;
  case 'clock':clock(c,0,72,205,p,progress*Math.PI*2);star(c,-275,-60,64,p.accent);text(c,'وقتك للمتابعة',0,370,46,p.ink,'center');break;
  case 'keyword':rect(c,-400,-25,800,240,28,p.ink);text(c,Campaign.keyword,0,126,95,p.bg,'center',700,735);text(c,'ابعتها في ماسنجر',0,310,47,p.ink,'center');star(c,320,-85,48,p.accent);break;
  case 'columns':rect(c,-380,-55,760,300,22,p.paper);text(c,'رقم الحساب / العقد / الخدمة',320,10,30,'#315237','right');text(c,'المبلغ',-220,10,31,'#315237','center');c.strokeStyle='#b6c9a9';c.lineWidth=2;c.beginPath();c.moveTo(35,40);c.lineTo(35,220);c.stroke();for(let i=0;i<3;i++){text(c,'رقم البحث',290,90+i*64,32,'#4b684b','right',400);text(c,'المبلغ',-185,90+i*64,32,'#4b684b','center',400);}text(c,'رقم ومبلغ، وبس',0,350,52,p.ink,'center');break;
  case 'types':['حساب','عقد','خدمة'].forEach((s,i)=>{rect(c,-350+i*235,-20,220,250,18,i===1?p.accent:p.paper);text(c,s,-240+i*235,126,59,'#264b36','center');check(c,-260+i*235,178,34,'#577b43');});text(c,'جهّز شيتك وجرّب',0,330,48,p.ink,'center');break;
  default:sheet(c,-325,-85,390,370,p,-.11,5);sheet(c,-55,-25,390,370,p,.11,5);star(c,330,-85,54,p.accent);text(c,'جرّب على شيتك',0,390,43,p.ink,'center');
 }c.restore();}
 function poster(canvas,post,format='feed'){const H=format==='story'?1920:1350;canvas.width=1080;canvas.height=H;const c=canvas.getContext('2d'),p=palettes[post.theme];c.fillStyle=p.bg;c.fillRect(0,0,1080,H);c.save();c.globalAlpha=.22;c.fillStyle=p.line;for(let row=0;row<8;row++)for(let col=0;col<7;col++){c.beginPath();c.arc(70+col*20,230+row*20,1.8,0,Math.PI*2);c.fill();}c.restore();c.strokeStyle=p.line;c.lineWidth=2;c.beginPath();c.arc(-130,H*.58,440,0,Math.PI*2);c.stroke();logo(c,p);text(c,post.tag,1010,222,30,p.muted,'right',500);post.title.forEach((t,i)=>text(c,t,1010,335+i*105,96,p.ink,'right',700,950));text(c,post.sub,1010,523,40,p.muted,'right',400,950);illustration(c,post.kind,p,H===1350?705:885,H===1350?.94:1.15);const bottom=H-248;c.strokeStyle=p.line;c.lineWidth=2;c.beginPath();c.moveTo(70,bottom);c.lineTo(1010,bottom);c.stroke();text(c,'ابعت «'+Campaign.keyword+'» في ماسنجر',540,bottom+77,44,p.ink,'center',700,950);text(c,'أول تشيكتين مجانًا، وبعدهم واحدة مجانية يوميًا',540,bottom+132,31,p.muted,'center',500,950);text(c,'لحد ٣٠٠٠ عميل / شيت · مدة العرض أسبوعين',540,bottom+181,31,p.muted,'center',400,950);}

 const duration=35;
 const clamp=v=>Math.max(0,Math.min(1,v));
 const ease=v=>1-Math.pow(1-clamp(v),4);
 const scenes=[
  {at:0,end:4,theme:'dark',kind:'focus',tag:'هدية تشيك للمحصّلين',title:['وراك متابعات؟','خلّي التشييك علينا.'],note:'وقتُك لمتابعة عملائك'},
  {at:4,end:9,theme:'paper',kind:'compare',tag:'تشيك بيعمل إيه؟',title:['نراجع شيت الإكسل','مع بيانات موقع زين.'],note:'أرقام الحسابات والعقود والخدمات'},
  {at:9,end:13,theme:'dark',kind:'gift',tag:'جرّب الخدمة على شيتك',title:['أول تشيكتين.','مجانًا.'],note:'كل تشيكة لشيت إكسل واحد'},
  {at:13,end:17,theme:'lime',kind:'scan',tag:'الشيت الكبير داخل في العرض',title:['لحد ٣٠٠٠ عميل.','في كل شيت.'],note:'حجم الشيت المشمول في كل تشيكة'},
  {at:17,end:23,theme:'dark',kind:'daily',tag:'بعد أول تشيكتين مجانًا',title:['تشيكة مجانية.','كل يوم.'],note:'لحد نهاية العرض، ومدته أسبوعين'},
  {at:23,end:29,theme:'paper',kind:'prepare',tag:'تجهّز شيتك إزاي؟',title:['جهّز عمودين:','رقم البحث والمبلغ.'],note:'اسم العميل اختياري'},
  {at:29,end:35,theme:'dark',kind:'message',tag:'ابدأ برسالة في ماسنجر صفحتنا',title:['ابعت «هدية تشيك».','واحجز دورك.'],note:'هنوضح المطلوب ونأكد معاك موعدك'}
 ];
 function backdrop(c,p,t=0){
  c.fillStyle=p.bg;c.fillRect(0,0,1080,1920);
  c.save();c.globalAlpha=.24;c.strokeStyle=p.line;c.lineWidth=2;
  [500,650,800].forEach((r,i)=>{c.beginPath();c.arc(-160,1050+i*100,r,0,Math.PI*2);c.stroke();});
  c.translate(970,850);c.rotate(t*.045);
  for(let i=0;i<12;i++){c.rotate(Math.PI/6);c.beginPath();c.moveTo(220,0);c.lineTo(250,0);c.stroke();}c.restore();
  c.save();c.globalAlpha=.25;c.fillStyle=p.line;
  for(let row=0;row<9;row++)for(let col=0;col<8;col++){c.beginPath();c.arc(65+col*21,230+row*21,1.8,0,Math.PI*2);c.fill();}c.restore();
 }
 function burst(c,p,t,x=540,y=995){
  const progress=clamp((t-.4)/1.9);if(progress<=0||progress>=1)return;
  c.save();c.globalAlpha=(1-progress)*.65;
  for(let i=0;i<24;i++){const a=i*2.39996,d=90+progress*360;c.save();c.translate(x+Math.cos(a)*d,y+Math.sin(a)*d*.7);c.rotate(a+progress*2);rect(c,-4,-12,8,24,3,i%3?p.accent:p.ink);c.restore();}c.restore();
 }
 function label(c,str,x,y,w,p,fill=p.paper,color='#315237'){
  if(fill===p.accent&&color===p.bg&&p.bg==='#f4f6e9')color=p.ink;
  rect(c,x-w/2,y-42,w,78,20,fill);text(c,str,x,y+8,32,color,'center',700,w-30);
 }
 function revealed(c,progress,draw,shift=55){
  const v=ease(progress);c.save();c.globalAlpha*=v;c.translate(0,(1-v)*shift);draw();c.restore();
 }
 function scanSheet(c,x,y,w,h,p,t,rotation=0){
  const progress=clamp(t/2.5);sheet(c,x,y,w,h,p,rotation,Math.floor(progress*5));
  c.save();c.translate(x+w/2,y+h/2);c.rotate(rotation);
  const yy=-h/2+90+progress*(h-125);
  const glow=c.createLinearGradient(0,yy-26,0,yy+26);glow.addColorStop(0,'rgba(150,197,102,0)');glow.addColorStop(.5,'rgba(108,159,60,.35)');glow.addColorStop(1,'rgba(150,197,102,0)');
  rect(c,-w/2+20,yy-26,w-40,52,4,glow);
  c.beginPath();c.moveTo(-w/2+20,yy);c.lineTo(w/2-20,yy);c.strokeStyle='#74a540';c.lineWidth=3;c.stroke();c.restore();
 }
 function animatedCalendar(c,p,t){
  const x=-410,y=-145,w=820,h=385;rect(c,x,y,w,h,28,p.paper);
  text(c,'مدة العرض أسبوعين',0,y+64,46,'#315237','center');
  const progress=clamp(t/2.9),cw=105,ch=108;
  for(let i=0;i<14;i++){
   const xx=330-(i%7)*112,yy=y+108+Math.floor(i/7)*125,v=ease(progress*16-i);
   c.save();c.translate(xx,yy);c.scale(.88+v*.12,.88+v*.12);rect(c,-46,0,96,ch,17,v>0?'#b7d882':'#e7eddd');
   text(c,arabic(i+1),0,56,38,'#254e37','center');if(v>.7)check(c,-9,75,19,'#46733e');c.restore();
  }
  label(c,'واحدة مجانية كل يوم',0,326,610,p,p.accent,p.bg);
  text(c,'لحد نهاية العرض للجميع',0,425,41,p.muted,'center',500,900);
 }
 function animatedColumns(c,p,t){
  const first=ease(t/.8),second=ease((t-.25)/.8);
  rect(c,-430,-155,860,435,30,p.paper,'#b6c9a9');
  text(c,'شيت العملاء',0,-84,39,'#315237','center');
  c.save();c.globalAlpha=first;rect(c,18,-35,390,258,18,'#dce9cf');
  text(c,'رقم الحساب / العقد / الخدمة',214,22,30,'#315237','center',700,355);
  for(let i=0;i<3;i++){rect(c,52,58+i*51,320,35,6,'#f4f6e9');text(c,'••••••••',215,83+i*51,26,'#486841','center',400);}c.restore();
  c.save();c.globalAlpha=second;rect(c,-408,-35,390,258,18,'#dce9cf');
  text(c,'المبلغ المطلوب مراجعته',-214,22,30,'#315237','center',700,355);
  for(let i=0;i<3;i++){rect(c,-375,58+i*51,320,35,6,'#f4f6e9');text(c,'المبلغ',-215,83+i*51,23,'#486841','center',400);}c.restore();
  revealed(c,(t-.65)/.65,()=>{label(c,'عمود رقم + عمود مبلغ',0,355,640,p,p.accent,p.ink);check(c,333,325,32,p.ink);});
  text(c,'اسم العميل اختياري',0,457,38,p.muted,'center',400);
 }
 function animatedMessage(c,p,t){
  const content=Campaign.keyword,slice=Array.from(content).slice(0,Math.floor(clamp((t-.3)/1.1)*Array.from(content).length)).join('');
  rect(c,-400,-180,800,540,35,p.paper);
  rect(c,-400,-180,800,94,[35,35,0,0],'#dce7ce');
  text(c,'رسالة إلى تشيك',315,-120,40,'#315237');
  rect(c,-345,-38,690,135,23,p.bg);text(c,slice||' ',0,52,74,p.ink,'center',700,630);
  if(t<1.7){c.fillStyle=p.accent;c.fillRect(-300,-12,3,71);}
  revealed(c,(t-1.1)/.65,()=>{rect(c,-340,135,680,149,20,'#dce7ce');text(c,'نراجع طلبك،',275,194,38,'#315237');text(c,'ونأكد معاك موعد التشييك.',275,248,36,'#315237','right',500,600);});
  label(c,'التنفيذ حسب دور الحجز',0,449,690,p,p.accent,p.bg);
 }

 function extraVisual(c,kind,p,t){
  const stages=['إرسال الشيت','تأكيد الموعد','نتيجة التشييك'];
  switch(kind){
   case 'tasks':case 'plan':case 'booking':{
    const items=kind==='tasks'?['تشييك الشيت','اتصالات العملاء','متابعة الحالات']:kind==='plan'?['متابعة العملاء','مكالماتك','ترتيب الأولويات']:stages;
    if(kind==='plan'){c.save();c.translate(-285,10);clock(c,0,0,120,p,t*.7);c.restore();}
    items.forEach((item,i)=>revealed(c,(t-i*.24)/.6,()=>{
     const x=kind==='plan'?-30:-370,w=kind==='plan'?420:740,y=-150+i*160;
     const fill=i===Math.min(2,Math.floor(t/1.1))?p.accent:p.paper,color=fill===p.accent&&p.bg==='#d0ec89'?p.bg:'#315237';
     rect(c,x,y,w,125,23,fill);
     text(c,item,x+w-80,y+78,43,color,'right',700,w-130);
     if(t>i*.5+.6)check(c,x+28,y+48,40,color);
    }));
    label(c,kind==='booking'?'نأكد الموعد حسب دور الحجز':kind==='plan'?'التشييك خلّيه لتشيك':'يومك مليان متابعات',0,416,740,p,p.accent,p.bg);break;
   }
   case 'calls':{
    rect(c,-240,-200,480,550,42,p.ink);rect(c,-219,-175,438,495,28,p.paper);
    text(c,'متابعات العملاء',0,-95,39,'#315237','center');
    const q=clamp(t/1.5);c.strokeStyle=p.line;c.lineWidth=3;[95,125,155].forEach((r,i)=>{c.save();c.globalAlpha=.25*(1-q)+.15;c.beginPath();c.arc(0,50,r+Math.sin(t+i)*8,0,Math.PI*2);c.stroke();c.restore();});
    rect(c,-74,-26,148,148,74,p.accent);text(c,'☎',0,74,85,p.bg,'center');
    text(c,'اتصال · متابعة',0,207,39,'#315237','center');
    rect(c,-137,245,274,45,20,'#c4db9d');check(c,-14,253,24,'#46733e');
    label(c,'وقتك لمتابعات عملائك',0,443,740,p,p.accent,p.bg);break;
   }
   case 'rules':{
    c.beginPath();c.arc(0,20,215,0,Math.PI*2);c.strokeStyle=p.line;c.lineWidth=16;c.stroke();
    c.beginPath();c.arc(0,20,215,-Math.PI/2,-Math.PI/2+ease(t/2)*Math.PI*2);c.strokeStyle=p.ink;c.lineWidth=16;c.stroke();
    text(c,'١٤',0,91,165,p.ink,'center');text(c,'يوم مدة العرض',0,160,37,p.muted,'center',500);
    label(c,'مدة واحدة للجميع',0,350,710,p,p.paper,'#315237');
    text(c,'تنتهي التشيكة اليومية بانتهاء العرض',0,449,35,p.ink,'center',500,940);break;
   }
   case 'file':{
    scanSheet(c,-210,-190,420,465,p,t,-.04);
    revealed(c,(t-.35)/.7,()=>{rect(c,-322,172,644,115,23,p.ink);text(c,'شيت العملاء.xlsx',0,247,51,p.bg,'center',700,580);});
    label(c,'عمود رقم + عمود مبلغ',0,405,730,p,p.accent,p.ink);break;
   }
   case 'numbers':{
    ['حساب','عقد','خدمة'].forEach((item,i)=>revealed(c,(t-i*.22)/.6,()=>{
     const x=260-i*260;rect(c,x-117,-80,234,240,25,p.paper);
     text(c,item,x,26,52,'#315237','center');text(c,'رقم البحث',x,85,28,'#486841','center',400);check(c,x-20,113,40,'#46733e');
     c.strokeStyle=p.line;c.lineWidth=3;c.beginPath();c.moveTo(x,180);c.lineTo(x,275);c.lineTo(0,275);c.stroke();
    }));
    label(c,'كلها في عمود الرقم',0,363,740,p,p.accent,p.bg);break;
   }
   case 'optional':{
    rect(c,-400,-168,800,480,30,p.paper);
    ['رقم البحث','المبلغ','اسم العميل (اختياري)'].forEach((item,i)=>{
     const y=-110+i*125;rect(c,-355,y,710,98,15,i===2?'#e7eddd':'#d5e5c2');
     text(c,item,283,y+62,39,'#315237','right',i===2?400:700,580);
     if(i<2&&t>i*.3+.4)check(c,-320,y+36,35,'#46733e');
    });
    label(c,'الرقم والمبلغ هما الأساس',0,430,790,p,p.ink,p.bg);break;
   }
   case 'sendfile':{
    rect(c,-400,-195,800,510,32,p.paper);text(c,'ماسنجر صفحتنا',310,-128,40,'#315237');
    revealed(c,t/.6,()=>{rect(c,-340,-55,680,108,20,p.ink);text(c,Campaign.keyword,0,18,60,p.bg,'center',700,620);});
    revealed(c,(t-.45)/.7,()=>{rect(c,-340,91,680,152,23,'#dce7ce');rect(c,227,118,77,91,13,'#8dbb61');text(c,'X',266,178,39,'#315237','center');text(c,'شيت العملاء.xlsx',170,157,39,'#315237','right',700,480);text(c,'ملف إكسل',170,211,29,'#486841','right',400);});
    label(c,'ابعت الكلمة وشيتك',0,421,740,p,p.accent,p.bg);break;
   }
   case 'result':{
    scanSheet(c,-280,-160,490,480,p,t,-.03);
    revealed(c,(t-.6)/.8,()=>{rect(c,96,82,280,210,25,p.ink);check(c,183,123,96,p.bg);text(c,'نتيجة التشييك',236,257,32,p.bg,'center',700,250);});
    label(c,'راجع النتيجة وكمل متابعاتك',0,432,820,p,p.accent,p.ink);break;
   }
  }
 }

 function sceneVisual(c,scene,p,t){
  c.save();c.translate(540,940);
  const drift=Math.sin(t*.9)*7;
  switch(scene.kind){
   case 'focus':
    c.save();c.translate(-205,drift);clock(c,0,5,180,p,t*.9);c.restore();
    revealed(c,(t-.1)/.6,()=>scanSheet(c,-20,-120,390,370,p,t,.06));
    revealed(c,(t-.5)/.6,()=>label(c,'متابعاتك محتاجة وقتك',0,347,740,p,p.accent,p.bg));
    break;
   case 'compare':{
    revealed(c,t/.7,()=>scanSheet(c,80,-135,325,370,p,t,.025));
    revealed(c,(t-.15)/.7,()=>{rect(c,-405,-135,325,370,24,p.ink);text(c,'بيانات زين',-243,-64,43,p.bg,'center',700,285);for(let i=0;i<5;i++){rect(c,-364,10+i*36,245,13,4,i<Math.floor(t*2)?p.accent:p.line);}});
    c.strokeStyle=p.line;c.lineWidth=4;c.beginPath();c.moveTo(68,35);c.bezierCurveTo(25,-30,-25,-30,-68,35);c.stroke();
    const q=(t*.65)%1;c.beginPath();c.arc(68-136*q,35-52*Math.sin(q*Math.PI),11,0,Math.PI*2);c.fillStyle=p.accent;c.fill();
    label(c,'شيتك',243,311,310,p);label(c,'موقع زين',-243,311,310,p,p.ink,p.bg);
    text(c,'نراجع الأرقام والمبالغ',0,426,42,p.muted,'center',500);break;
   }
   case 'gift':
    c.save();c.translate(-ease(t/.7)*30,drift);scanSheet(c,-335,-65,355,345,p,t,-.08);c.restore();
    c.save();c.translate(ease((t-.2)/.7)*30,-drift);scanSheet(c,-10,-30,355,345,p,t,.08);c.restore();
    revealed(c,(t-.3)/.7,()=>{rect(c,-117,-161,234,170,28,p.accent);text(c,'٢',0,-24,154,p.bg==='#f4f6e9'?p.ink:p.bg,'center');});
    label(c,'أول تشيكتين علينا',0,419,680,p,p.accent,p.bg);break;
   case 'scan':{
    scanSheet(c,-230,-175,460,470,p,t,-.03);
    const count=Math.round(ease(t/1.15)*3000);rect(c,-390,12,780,185,26,p.ink);
    text(c,arabic(count),0,153,165,p.bg,'center');text(c,'عميل لكل شيت',0,299,51,p.ink,'center');
    label(c,'كل تشيكة = شيت واحد',0,417,740,p,p.paper,'#315237');break;
   }
   case 'daily':animatedCalendar(c,p,t);break;
   case 'prepare':animatedColumns(c,p,t);break;
   case 'message':animatedMessage(c,p,t);break;
   default:extraVisual(c,scene.kind,p,t);break;
  }
  c.restore();
  if(scene.kind==='gift'||scene.kind==='message')burst(c,p,t);
 }
 function renderScene(c,scene,seconds,sceneList=scenes){
  const p=palettes[scene.theme],t=Math.max(0,seconds-scene.at);
  backdrop(c,p,seconds);logo(c,p);
  text(c,'هدية تشيك',80,123,27,p.muted,'left',500);
  // Stagger each line; allow a legible static frame at time zero.
  revealed(c,Math.max(.2,t/.55),()=>text(c,scene.tag,540,294,33,p.muted,'center',500,940),24);
  scene.title.forEach((line,i)=>revealed(c,Math.max(.18,(t-i*.11)/.65),()=>{
    text(c,line,540,424+i*119,i===1?100:89,i===1&&scene.theme==='dark'?p.accent:p.ink,'center',700,960);
  },48));
  revealed(c,Math.max(.2,(t-.2)/.6),()=>text(c,scene.note,540,641,38,p.muted,'center',500,940),24);
  revealed(c,Math.max(.16,(t-.15)/.8),()=>sceneVisual(c,scene,p,t),75);
  // Fixed offer footer stays on screen; details do not disappear between scenes.
  c.strokeStyle=p.line;c.lineWidth=2;c.beginPath();c.moveTo(85,1451);c.lineTo(995,1451);c.stroke();
  text(c,'أول تشيكتين مجانًا، وبعدهم واحدة مجانية يوميًا',540,1537,35,p.ink,'center',700,950);
  text(c,'لحد ٣٠٠٠ عميل / شيت · مدة العرض أسبوعين',540,1606,34,p.muted,'center',500,950);
  text(c,'التشيكة اليومية لحد نهاية العرض',540,1665,33,p.muted,'center',500,940);
  text(c,scene.kind==='message'?'ابعت «'+Campaign.keyword+'» في ماسنجر':'تشيك · وقتك للمتابعة',540,1764,42,p.ink,'center',700,950);
  sceneList.forEach((s,i)=>{const span=900/sceneList.length,xx=990-i*span,completed=seconds>=s.end,active=seconds>=s.at&&seconds<s.end;rect(c,xx-span+18,1810,span-18,6,3,p.line);if(completed||active)rect(c,xx-span+18,1810,(span-18)*(completed?1:clamp((seconds-s.at)/(s.end-s.at))),6,3,p.accent);});
  text(c,arabic(sceneList.indexOf(scene)+1)+' / '+arabic(sceneList.length),540,1880,25,p.muted,'center',400);
 }
 function film(canvas,seconds,videoId='overview'){
  const sceneList=(window.CampaignVideos||[]).find(v=>v.id===videoId)?.scenes||scenes;
  const c=canvas.getContext('2d'),time=Math.min(duration,Math.max(0,seconds)),scene=sceneList.find(s=>time<s.end)||sceneList.at(-1),local=time-scene.at;
  c.save();c.scale(canvas.width/1080,canvas.height/1920);renderScene(c,scene,time,sceneList);
  // A moving sheet-like wipe links scenes without flashing or obscuring the copy for long.
  if(scene.at>0&&local<.42){const p=palettes[scene.theme],v=ease(local/.42);c.save();c.globalAlpha=(1-v)*.9;c.translate(-1080*v,0);c.fillStyle=p.accent;c.beginPath();c.moveTo(0,0);c.lineTo(1080,0);c.lineTo(990,1920);c.lineTo(0,1920);c.closePath();c.fill();c.restore();}
  c.restore();
 }
 return {poster,film,logo,palettes,scenes,duration};
})();
