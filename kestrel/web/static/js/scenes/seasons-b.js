/* Seasonal touches B: civic, lights and lanterns. Overlays drawn on top of any scene. Every id/class/keyframe is prefixed ss-<id>-. */
window.SEASONS=window.SEASONS||[];
(function(){
const f1=n=>Math.round(n*10)/10;
function rng(seed){let s=seed>>>0;return()=>{s=(s+0x6D2B79F5)>>>0;let t=s;t=Math.imul(t^(t>>>15),t|1);t=(t+Math.imul(t^(t>>>7),t|61))^t;return((t^(t>>>14))>>>0)/4294967296;};}
const wrap=(id,css,defs,body)=>`<style>@media (prefers-reduced-motion:no-preference){${css}}</style>${defs?`<defs>${defs}</defs>`:''}${body}`;
const glowDef=(id,col,name='glow')=>`<radialGradient id="ss-${id}-${name}"><stop offset="0" stop-color="${col}" stop-opacity=".9"/><stop offset="1" stop-color="${col}" stop-opacity="0"/></radialGradient>`;

/* A radial firework burst centered on cx,cy with outer radius R. */
function burst(id,cx,cy,R,col,n,delay,glow){
 let o=glow?`<circle cx="${cx}" cy="${cy}" r="${f1(R*1.05)}" fill="${glow}" opacity=".26"/>`:'';
 const off=(cx*7+cy)%10/10;
 for(let i=0;i<n;i++){const a=(i+off)/n*Math.PI*2,c=Math.cos(a),s=Math.sin(a),long=i%2===0,r1=R*(long?.38:.52),r2=R*(long?1:.78);
  o+=`<path d="M${f1(cx+c*r1)} ${f1(cy+s*r1)} L${f1(cx+c*r2)} ${f1(cy+s*r2)}" stroke="${col}" stroke-width="1.2" stroke-linecap="round" fill="none"/><circle cx="${f1(cx+c*(r2+2))}" cy="${f1(cy+s*(r2+2))}" r="${long?1.2:.9}" fill="${col}"/>`;}
 o+=`<circle cx="${cx}" cy="${cy}" r="1.6" fill="${col}"/>`;
 return `<g class="ss-${id}-burst" style="animation-delay:${delay}s">${o}</g>`;
}
const burstCss=(id,dur=5.5)=>`.ss-${id}-burst{animation:ss-${id}-b ${dur}s ease-out infinite;transform-box:fill-box;transform-origin:center}@keyframes ss-${id}-b{0%{transform:scale(.15);opacity:0}12%{opacity:1}55%{transform:scale(1);opacity:.95}100%{transform:scale(1.14);opacity:0}}`;

/* ---------- 1. New Year ---------- */
function drawNewYear(){
 const id='newyear',p=`ss-${id}-`;
 const css=burstCss(id,6)+`.${p}ball{animation:${p}drop 12s ease-in-out infinite}@keyframes ${p}drop{0%{transform:translateY(0);opacity:0}6%{opacity:1}70%{transform:translateY(84px);opacity:1}88%{transform:translateY(84px);opacity:1}100%{transform:translateY(84px);opacity:0}}
.${p}spark{animation:${p}sp 12s ease-out infinite;transform-box:fill-box;transform-origin:center}@keyframes ${p}sp{0%,68%{transform:scale(.2);opacity:0}74%{opacity:1}94%,100%{transform:scale(1.5);opacity:0}}`;
 let b=burst(id,248,28,17,'var(--lite)',12,0)+burst(id,306,50,12,'var(--aur2)',10,-2.2)+burst(id,508,24,16,'var(--aur1)',12,-4)+burst(id,330,20,9,'var(--bad)',8,-3.1);
 b+=`<path d="M549 14 V108" stroke="var(--muted)" stroke-width="1.2" opacity=".7"/><circle cx="549" cy="13" r="1.8" fill="var(--muted)" opacity=".8"/>`;
 b+=`<g class="${p}ball"><circle cx="549" cy="20" r="8" fill="var(--lite)" opacity=".3"/><circle cx="549" cy="20" r="4.3" fill="var(--lite)"/><circle cx="547.8" cy="18.8" r="1.3" fill="var(--paper)" opacity=".9"/></g>`;
 let sp='';for(let i=0;i<8;i++){const a=i/8*Math.PI*2;sp+=`<path d="M${f1(549+Math.cos(a)*4)} ${f1(104+Math.sin(a)*4)} L${f1(549+Math.cos(a)*9)} ${f1(104+Math.sin(a)*9)}" stroke="var(--lite)" stroke-width="1.3" stroke-linecap="round"/>`;}
 b+=`<g class="${p}spark" opacity="0">${sp}</g>`;
 return wrap(id,css,'',b);
}
/* ---------- 2. Fourth of July ---------- */
function drawJuly4(){
 const id='july4';
 const css=burstCss(id,5.5);
 let b=burst(id,246,30,17,'var(--bad)',12,0)+burst(id,300,52,12,'var(--paper)',10,-1.8,'var(--brand)')+burst(id,510,26,17,'var(--brand)',12,-3.6)+burst(id,336,18,10,'var(--paper)',9,-2.6,'var(--brand)')+burst(id,540,56,11,'var(--bad)',9,-4.6);
 return wrap(id,css,'',b);
}
/* ---------- 3. Halloween ---------- */
function bat(){return `<path d="M0 0 L1.3 -2.6 L2.2 -.4 C4 -2.8 7 -3.4 10 -1.6 C8.4 -.4 8.2 .8 7 2 C6 1 5 1 4 2 C3 1 2.2 1 1.6 2 L0 3.2 L-1.6 2 C-2.2 1 -3 1 -4 2 C-5 1 -6 1 -7 2 C-8.2 .8 -8.4 -.4 -10 -1.6 C-7 -3.4 -4 -2.8 -2.2 -.4 L-1.3 -2.6Z" fill="var(--bird)"/>`;}
function drawHalloween(){
 const id='halloween',p=`ss-${id}-`;
 const css=`.${p}fly{animation:${p}fl 15s linear infinite}@keyframes ${p}fl{0%{transform:translate(0,0);opacity:0}8%{opacity:1}22%{transform:translate(34px,-9px)}40%{transform:translate(70px,7px)}58%{transform:translate(104px,-8px)}76%{transform:translate(140px,5px)}92%{opacity:1}100%{transform:translate(172px,-4px);opacity:0}}
.${p}flap{animation:${p}fp 1.4s ease-in-out infinite alternate;transform-box:fill-box;transform-origin:center}@keyframes ${p}fp{to{transform:scaleY(.55)}}
.${p}moon{animation:${p}mn 8s ease-in-out infinite alternate}@keyframes ${p}mn{to{opacity:.75;transform:scale(1.06)}}.${p}moon,.${p}pk{transform-box:fill-box;transform-origin:center}
.${p}pk{animation:${p}pk 3.2s ease-in-out infinite alternate}@keyframes ${p}pk{from{opacity:.45}to{opacity:1}}`;
 const defs=glowDef(id,'var(--lite)')+glowDef(id,'var(--lite)','pg')+glowDef(id,'var(--glow)','mg');
 let b=`<g class="${p}moon"><circle cx="276" cy="86" r="46" fill="url(#${p}mg)" opacity=".85"/><circle cx="276" cy="86" r="17" fill="var(--orb)"/><circle cx="270" cy="82" r="3" fill="var(--glow)" opacity=".5"/><circle cx="281" cy="91" r="2" fill="var(--glow)" opacity=".4"/></g>`;
 [[200,70,0],[226,84,-5],[206,56,-10]].forEach(([x,y,d])=>{b+=`<g transform="translate(${x} ${y})"><g class="${p}fly" style="animation-delay:${d}s"><g class="${p}flap" style="animation-delay:${d*.37}s">${bat()}</g></g></g>`;});
 [[292,105],[520,105]].forEach(([x,y],k)=>{
  b+=`<g class="${p}pk" style="animation-delay:${-k*1.6}s"><circle cx="${x}" cy="${y}" r="15" fill="url(#${p}pg)" opacity=".7"/></g><ellipse cx="${x}" cy="${y+1}" rx="9" ry="7.2" fill="var(--lite)"/><path d="M${x-1} ${y-6.4} q1 -3 3.4 -3.4 q-.6 2 -.4 3.4Z" fill="var(--z3)"/><path d="M${x-5} ${y-2.4} l2.6 -2.4 l1.8 2.6Z M${x+5} ${y-2.4} l-2.6 -2.4 l-1.8 2.6Z M${x-4.6} ${y+3} q4.6 4 9.2 0 q-1 1.6 -2.4 1.2 q-1.2 -.2 -2.2 .8 q-1 -1 -2.2 -.8 q-1.4 .4 -2.4 -1.2Z" fill="var(--tree)"/>`;});
 return wrap(id,css,defs,b);
}
/* ---------- 4. Lunar New Year ---------- */
function drawLunar(){
 const id='lunarny',p=`ss-${id}-`;
 const css=`.${p}lan{animation:${p}sw 4s ease-in-out infinite alternate;transform-origin:top;transform-box:fill-box}@keyframes ${p}sw{from{transform:rotate(-5.5deg)}to{transform:rotate(5.5deg)}}
.${p}gl{animation:${p}gl 3.4s ease-in-out infinite alternate}@keyframes ${p}gl{from{opacity:.3}to{opacity:.75}}`;
 const lan=(x,len,d)=>`<g class="${p}lan" style="animation-delay:${d}s"><circle class="${p}gl" cx="${x}" cy="${len+15}" r="24" fill="url(#${p}glow)" style="animation-delay:${d}s"/><line x1="${x}" y1="0" x2="${x}" y2="${len}" stroke="var(--lite)" stroke-width="1.2"/><rect x="${x-3}" y="${len}" width="6" height="3" fill="var(--lite)"/><ellipse cx="${x}" cy="${len+15}" rx="11.5" ry="12.5" fill="var(--bad)"/><path d="M${x-5.5} ${len+4} q-3 11 0 22 M${x+5.5} ${len+4} q3 11 0 22 M${x} ${len+3} v24" stroke="var(--paper)" stroke-width="1" fill="none" opacity=".45"/><rect x="${x-3}" y="${len+27}" width="6" height="3" fill="var(--lite)"/><path d="M${x} ${len+30} v9 M${x-1.6} ${len+30} v7 M${x+1.6} ${len+30} v7" stroke="var(--lite)" stroke-width="1.1" fill="none"/></g>`;
 return wrap(id,css,glowDef(id,'var(--bad)'),lan(228,2,0)+lan(272,16,-1.3)+lan(514,4,-2.1)+lan(546,20,-.7));
}
/* ---------- 5. Labor Day ---------- */
function drawLabor(){
 const id='laborday',p=`ss-${id}-`;
 const css=`.${p}fl{animation:${p}fl 3s ease-in-out infinite alternate;transform-origin:top;transform-box:fill-box}@keyframes ${p}fl{from{transform:skewX(7deg) scaleY(1)}to{transform:skewX(-8deg) scaleY(.92)}}`;
 const cols=['var(--brand)','var(--lite)','var(--bad)','var(--z3)'];
 const swag=(x0,x1,sag,n,off)=>{const mx=(x0+x1)/2;let o=`<path d="M${x0} 3 Q${mx} ${3+sag*2} ${x1} 3" stroke="var(--muted)" stroke-width="1.1" fill="none" opacity=".8"/>`;
  for(let k=0;k<n;k++){const t=(k+.5)/n,x=(1-t)*(1-t)*x0+2*t*(1-t)*mx+t*t*x1,y=(1-t)*(1-t)*3+2*t*(1-t)*(3+sag*2)+t*t*3;
   o+=`<path class="${p}fl" style="animation-delay:${f1(-(k+off)*.45)}s" d="M${f1(x-6)} ${f1(y)} L${f1(x+6)} ${f1(y)} L${f1(x)} ${f1(y+13)}Z" fill="${cols[(k+off)%4]}"/>`;}
  return o;};
 return wrap(id,css,'',swag(190,350,11,10,0)+swag(488,580,10,6,2));
}
/* ---------- 6. Colors of spring ---------- */
function drawColors(){
 const id='colors',p=`ss-${id}-`;
 const r=rng(31),cols=['var(--z1)','var(--z2)','var(--z3)','var(--aur1)','var(--aur2)'];
 let b='';
 for(let i=0;i<8;i++){const x=205+r()*350,y=10+r()*52,rad=6+r()*6,dur=9+r()*5,dl=-r()*dur;
  b+=`<circle class="${p}puff" cx="${f1(x)}" cy="${f1(y)}" r="${f1(rad*1.5)}" fill="url(#${p}c${i%5})" opacity=".7" style="animation-duration:${f1(dur)}s;animation-delay:${f1(dl)}s"/>`;}
 for(let i=0;i<14;i++){const x=200+r()*360,y=4+r()*60,dx=(r()<.5?-1:1)*(14+r()*22),dur=8+r()*5,dl=-r()*dur;
  b+=`<ellipse class="${p}pet" cx="${f1(x)}" cy="0" rx="3.2" ry="1.9" fill="${cols[(i+2)%5]}" style="--y:${f1(y)}px;--dx:${f1(dx)}px;animation-duration:${f1(dur)}s;animation-delay:${f1(dl)}s"/>`;}
 const css=`.${p}puff{animation:${p}pf 11s ease-in-out infinite alternate;transform-box:fill-box;transform-origin:center}@keyframes ${p}pf{from{transform:translate(-8px,6px) scale(.8);opacity:.2}to{transform:translate(10px,-8px) scale(1.25);opacity:.45}}
.${p}pet{animation:${p}pt 10s linear infinite;transform:translateY(var(--y));transform-box:fill-box;transform-origin:center}@keyframes ${p}pt{0%{transform:translate(0,calc(var(--y) * -1 + 2px)) rotate(0);opacity:0}12%{opacity:.9}88%{opacity:.9}100%{transform:translate(var(--dx),calc(76px - var(--y))) rotate(260deg);opacity:0}}`;
 return wrap(id,css,cols.map((c,k)=>glowDef(id,c,'c'+k)).join(''),b);
}
/* ---------- 7. Crescent evenings ---------- */
function drawCrescent(){
 const id='crescent',p=`ss-${id}-`;
 const css=`.${p}lan{animation:${p}sw 5s ease-in-out infinite alternate;transform-origin:top;transform-box:fill-box}@keyframes ${p}sw{from{transform:rotate(-4deg)}to{transform:rotate(4deg)}}
.${p}gl{animation:${p}gl 4s ease-in-out infinite alternate}@keyframes ${p}gl{from{opacity:.4}to{opacity:1}}.${p}moon{animation:${p}mg 8s ease-in-out infinite alternate}@keyframes ${p}mg{to{opacity:.55}}`;
 const defs=glowDef(id,'var(--lite)');
 const lan=(x,len,d,s)=>`<g class="${p}lan" style="animation-delay:${d}s"><circle cx="${x}" cy="${len+13*s}" r="${f1(17*s)}" fill="url(#${p}glow)" opacity=".3"/><circle class="${p}gl sc-night" cx="${x}" cy="${len+13*s}" r="${f1(19*s)}" fill="url(#${p}glow)" style="animation-delay:${d}s"/><line x1="${x}" y1="0" x2="${x}" y2="${len}" stroke="var(--lite)" stroke-width="1.1"/><path d="M${x-2.5} ${len+3*s} L${x+2.5} ${len+3*s} L${x+1.5} ${len} L${x-1.5} ${len}Z" fill="var(--tree)"/><path d="M${x-2.5} ${f1(len+3*s)} Q${x-9*s} ${f1(len+9*s)} ${x-8*s} ${f1(len+16*s)} Q${x-7*s} ${f1(len+23*s)} ${x} ${f1(len+25*s)} Q${x+7*s} ${f1(len+23*s)} ${x+8*s} ${f1(len+16*s)} Q${x+9*s} ${f1(len+9*s)} ${x+2.5} ${f1(len+3*s)}Z" fill="var(--lite)" opacity=".82"/><path d="M${x} ${f1(len+3*s)} V${f1(len+25*s)} M${f1(x-8*s)} ${f1(len+14*s)} H${f1(x+8*s)}" stroke="var(--tree)" stroke-width="1" opacity=".55" fill="none"/><path d="M${x-2} ${f1(len+25*s)} L${x+2} ${f1(len+25*s)} L${x} ${f1(len+29*s)}Z" fill="var(--tree)"/></g>`;
 let b=`<g class="${p}moon"><circle cx="318" cy="24" r="17" fill="url(#${p}glow)" opacity=".35"/></g><path d="M318 15.5 A8.5 8.5 0 1 0 326.2 27.2 A6.8 6.8 0 1 1 318 15.5Z" fill="var(--lite)"/>`;
 b+=lan(222,2,0,1)+lan(262,18,-1.7,.9)+lan(512,3,-2.4,1)+lan(544,22,-.8,.9);
 return wrap(id,css,defs,b);
}
/* ---------- 8. Lantern night ---------- */
function drawLanterns(){
 const id='lanterns',p=`ss-${id}-`;
 const css=`.${p}fl{animation:${p}fk 2.6s ease-in-out infinite alternate;transform-box:fill-box;transform-origin:50% 100%}@keyframes ${p}fk{from{transform:scale(.85,.8);opacity:.7}to{transform:scale(1.1,1.2);opacity:1}}
.${p}gl{animation:${p}gg 2.6s ease-in-out infinite alternate}@keyframes ${p}gg{from{opacity:.4}to{opacity:.9}}
.${p}up{animation:${p}rise 15s ease-in-out infinite;opacity:0}@keyframes ${p}rise{0%{transform:translate(0,0);opacity:0}15%{opacity:1}80%{opacity:1}100%{transform:translate(var(--dx),-64px);opacity:0}}`;
 let b='';
 for(let k=0;k<15;k++){const x=206+k*23.5,y=106.5,d=f1(-((k*7)%13)*.37);
  b+=`<circle class="${p}gl" cx="${f1(x)}" cy="${y-2}" r="11" fill="url(#${p}glow)" style="animation-delay:${d}s"/><path d="M${f1(x-4.2)} ${y} q4.2 5 8.4 0Z" fill="var(--z2)"/><path class="${p}fl" d="M${f1(x)} ${y-6.4} q2 3 0 6.4 q-2 -3 0 -6.4Z" fill="var(--lite)" style="animation-delay:${d}s"/>`;}
 [[236,86,-14,0],[300,92,10,-4],[518,88,-12,-8],[548,94,8,-11]].forEach(([x,y,dx,d])=>{
  b+=`<g class="${p}up" style="--dx:${dx}px;animation-delay:${d}s"><circle cx="${x}" cy="${y}" r="15" fill="url(#${p}glow)" opacity=".7"/><path d="M${x-4} ${y+5} L${x-3} ${y-4} Q${x} ${y-8} ${x+3} ${y-4} L${x+4} ${y+5}Z" fill="var(--lite)"/><path d="M${x-4} ${y+5} L${x+4} ${y+5}" stroke="var(--z2)" stroke-width="1.3"/></g>`;});
 return wrap(id,css,glowDef(id,'var(--lite)'),b);
}
/* ---------- 9. Winter lights ---------- */
function drawWinterLights(){
 const id='winterlights',p=`ss-${id}-`;
 const cols=['var(--bad)','var(--lite)','var(--aur1)','var(--aur2)','var(--z1)'];
 const css=`.${p}b{animation:${p}tw 4s ease-in-out infinite;transform-box:fill-box;transform-origin:center}@keyframes ${p}tw{0%,100%{opacity:.4;transform:scale(.9)}10%{opacity:1;transform:scale(1.3)}28%{opacity:.7;transform:scale(1)}}`;
 let b='',idx=0;
 const swag=(x0,x1,sag,n)=>{const mx=(x0+x1)/2;b+=`<path d="M${x0} 2 Q${mx} ${2+sag*2} ${x1} 2" stroke="var(--muted)" stroke-width="1" fill="none" opacity=".7"/>`;
  for(let k=0;k<n;k++){const t=(k+.5)/n,x=(1-t)*(1-t)*x0+2*t*(1-t)*mx+t*t*x1,y=(1-t)*(1-t)*2+2*t*(1-t)*(2+sag*2)+t*t*2,c=cols[idx%5],d=f1(-idx*.14);idx++;
   b+=`<g class="${p}b" style="animation-delay:${d}s"><circle cx="${f1(x)}" cy="${f1(y+4.5)}" r="9" fill="url(#${p}g${idx%5})" opacity=".8"/><path d="M${f1(x-1.6)} ${f1(y)} h3.2 v2.4 h-3.2Z" fill="var(--muted)"/><ellipse cx="${f1(x)}" cy="${f1(y+6)}" rx="3.4" ry="4.2" fill="${c}"/><ellipse cx="${f1(x-1)}" cy="${f1(y+4.8)}" rx=".9" ry="1.3" fill="var(--paper)" opacity=".7"/></g>`;}};
 swag(188,302,8,8);swag(302,412,8,8);swag(412,524,8,8);swag(524,590,7,5);
 return wrap(id,css,cols.map((c,k)=>glowDef(id,c,'g'+k)).join(''),b);
}

SEASONS.push({id:'newyear',name:'New Year',group:'Civic',when:'Dec 31 to Jan 1',moves:'Soft fireworks bloom, a glowing ball drops down a pole and sparkles',draw:drawNewYear});
SEASONS.push({id:'july4',name:'Fourth of July',group:'Civic',when:'Around July 4',moves:'Red, white and blue fireworks bloom',draw:drawJuly4});
SEASONS.push({id:'halloween',name:'Halloween',group:'Civic',when:'Around October 31',moves:'Bats flit by, a big low moon glows, two pumpkins flicker',draw:drawHalloween});
SEASONS.push({id:'lunarny',name:'Lunar New Year',group:'Civic',when:'Varies, late January to February',moves:'Red lanterns swing from the top edge',draw:drawLunar});
SEASONS.push({id:'laborday',name:'Labor Day',group:'Civic',when:'First Monday of September',moves:'Bunting flutters across the top',draw:drawLabor});
SEASONS.push({id:'colors',name:'Colors of spring',group:'Lights and lanterns',when:'March, dates vary',moves:'Soft powder puffs and petals drift by',draw:drawColors});
SEASONS.push({id:'crescent',name:'Crescent evenings',group:'Lights and lanterns',when:'A month, dates vary',moves:'A thin crescent glows, lanterns sway and warm up at night',draw:drawCrescent});
SEASONS.push({id:'lanterns',name:'Lantern night',group:'Lights and lanterns',when:'Autumn, dates vary',moves:'Small oil lamps flicker, sky lanterns rise',draw:drawLanterns});
SEASONS.push({id:'winterlights',name:'Winter lights',group:'Lights and lanterns',when:'Dec 10 to Jan 2',moves:'A string of lights twinkles in sequence',draw:drawWinterLights});
})();
