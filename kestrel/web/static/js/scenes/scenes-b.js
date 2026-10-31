/* Scenes B: winter, canyon, island, aurora, rain, savanna. Every id/class/gradient is prefixed sc-<id>-. */
window.SCENES=window.SCENES||[];
(function(){
const W=560,H=110;
const f1=n=>Math.round(n*10)/10;
function rng(seed){let s=seed>>>0;return()=>{s=(s+0x6D2B79F5)>>>0;let t=s;t=Math.imul(t^(t>>>15),t|1);t=(t+Math.imul(t^(t>>>7),t|61))^t;return((t^(t>>>14))>>>0)/4294967296;};}
/* Shared frame: motion css, left fade mask, sky object with halo. */
function frame(id,css,defs,body){
 const p='sc-'+id+'-';
 return `<style>@media (prefers-reduced-motion:no-preference){${css}}</style><defs><linearGradient id="${p}fade" x1="0" x2="1"><stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset=".4" stop-color="#fff"/></linearGradient><mask id="${p}mask"><rect width="${W}" height="${H}" fill="url(#${p}fade)"/></mask><radialGradient id="${p}glow"><stop offset="0" stop-color="var(--glow)" stop-opacity=".9"/><stop offset="1" stop-color="var(--glow)" stop-opacity="0"/></radialGradient>${defs}</defs><g mask="url(#${p}mask)">${body}</g>`;
}
function orb(id,x,y,r=11,halo=30){return `<circle class="sc-${id}-halo" cx="${x}" cy="${y}" r="${halo}" fill="url(#sc-${id}-glow)"/><circle cx="${x}" cy="${y}" r="${r}" fill="var(--orb)"/>`;}
const haloCss=id=>`.sc-${id}-halo{animation:sc-${id}-breathe 7s ease-in-out infinite;transform-box:fill-box;transform-origin:center}@keyframes sc-${id}-breathe{50%{opacity:.4;transform:scale(1.16)}}`;
/* Three-tier evergreen. h = total height. */
function pine(x,base,h,fill,cls,delay,snow){
 let d='',sn='';
 for(let i=0;i<3;i++){const top=base-h+i*h*.27,bot=top+h*.44,hw=h*(.15+i*.08);
  d+=`M${f1(x)} ${f1(top)} L${f1(x+hw)} ${f1(bot)} L${f1(x-hw)} ${f1(bot)}Z`;
  if(snow)sn+=`M${f1(x)} ${f1(top)} L${f1(x+hw*.5)} ${f1(top+(bot-top)*.5)} L${f1(x-hw*.5)} ${f1(top+(bot-top)*.5)}Z`;}
 d+=`M${f1(x-h*.03)} ${f1(base-h*.12)} h${f1(h*.06)} v${f1(h*.12)} h${f1(-h*.06)}Z`;
 return `<g class="${cls}" style="animation-delay:${delay}s"><path d="${d}" fill="${fill}"/>${sn?`<path d="${sn}" fill="var(--snow)" opacity=".75"/>`:''}</g>`;
}

/* 1. WINTER */
function sceneWinter(){
 const id='winter',p='sc-winter-';
 const r=rng(11);let flakes='';
 for(let i=0;i<24;i++){const x=200+r()*360,y=6+r()*92,dx=-10-r()*12,dur=7+r()*5,dl=-r()*dur,s=1.5+r()*1.1,dayOnly=i%3===2;
  flakes+=`<circle class="${p}flake${dayOnly?' sc-day':''}" cx="${f1(x)}" cy="0" r="${f1(s)}" fill="${dayOnly?'var(--m2s)':'var(--snow)'}" style="--y:${f1(y)}px;--dx:${f1(dx)}px;animation-duration:${f1(dur)}s;animation-delay:${f1(dl)}s"/>`;}
 const css=haloCss(id)+`.${p}flake{animation:${p}fall 12s linear infinite;transform:translateY(var(--y))}
@keyframes ${p}fall{0%{transform:translate(0,calc(var(--y) * -1 + 4px));opacity:0}12%{opacity:1}88%{opacity:1}100%{transform:translate(var(--dx),calc(104px - var(--y)));opacity:0}}
.${p}smoke{animation:${p}rise 7s ease-out infinite;transform-box:fill-box;transform-origin:center;opacity:0}
@keyframes ${p}rise{0%{transform:translate(0,0) scale(.6);opacity:0}15%{opacity:.9}100%{transform:translate(-26px,-44px) scale(3);opacity:0}}
.${p}sway{animation:${p}sw 6s ease-in-out infinite alternate;transform-origin:bottom;transform-box:fill-box}
@keyframes ${p}sw{to{transform:skewX(3.5deg)}}
.${p}glow{animation:${p}lit 9s ease-in-out infinite}@keyframes ${p}lit{50%{opacity:.45}}`;
 let b=orb(id,412,32);
 b+=`<path d="M0 110 L0 72 L190 72 C230 62 262 52 300 56 C340 60 362 42 402 48 C448 54 480 38 520 44 C540 46 552 42 560 44 L560 110Z" fill="var(--m1)"/>`;
 b+=`<path d="M402 48 C448 54 480 38 520 44 C540 46 552 42 560 44 L560 70 C520 62 470 66 440 62 C420 60 410 54 402 48Z" fill="var(--m1s)" opacity=".55"/>`;
 b+=`<path d="M0 110 L0 86 L190 86 C240 78 290 70 340 74 C380 78 410 64 450 64 C490 64 520 60 560 62 L560 110Z" fill="var(--m2)"/><path d="M340 74 C380 78 410 64 450 64 C490 64 520 60 560 62 L560 74 C520 74 490 78 450 78 C410 78 380 90 340 74Z" fill="var(--m2s)" opacity=".5"/>`;
 [[330,76,12],[342,78,16],[360,76,11],[500,66,12],[514,67,16],[540,64,13]].forEach(([x,y,h],k)=>b+=pine(x,y,h,'var(--tree3)',p+'sway',-k*.8,false));
 b+=`<path d="M0 110 L0 100 L190 100 C260 90 320 98 380 91 C430 85 480 88 520 84 C540 82 550 84 560 82 L560 110Z" fill="var(--snow)"/><path d="M0 110 L0 104 L190 104 C280 104 360 108 440 100 C500 94 530 98 560 94 L560 110Z" fill="var(--m1)" opacity=".5"/>`;
 /* cabin */
 b+=`<g><ellipse cx="452" cy="88" rx="30" ry="3.5" fill="var(--m1)" opacity=".6"/><rect x="436" y="73" width="32" height="14" fill="var(--m3s)"/><rect x="458" y="73" width="10" height="14" fill="var(--m3)" opacity=".45"/><rect x="462" y="58" width="5" height="11" fill="var(--m3s)"/><rect x="461" y="56.5" width="7" height="2.5" fill="var(--snow)"/><path d="M431 74 L452 57 L473 74Z" fill="var(--tree)"/><path d="M430 74.5 L452 55.5 L474 74.5 L469.5 75 L452 61 L434.5 75Z" fill="var(--snow)"/><rect x="443" y="77" width="7" height="6" fill="var(--m2s)"/><rect class="sc-night ${p}glow" x="443" y="77" width="7" height="6" fill="var(--lite)"/><circle class="sc-night ${p}glow" cx="446.5" cy="80" r="8" fill="var(--lite)" opacity=".18"/><rect x="455" y="77" width="6" height="10" fill="var(--tree)"/></g>`;
 for(let k=0;k<3;k++)b+=`<circle class="${p}smoke" cx="464.5" cy="55" r="3.2" fill="var(--snow)" style="animation-delay:${-k*2.33}s"/>`;
 [[392,92,22],[406,94,30],[498,92,26],[514,95,36],[536,93,24],[360,92,16],[348,93,22]].forEach(([x,y,h],k)=>b+=pine(x,y,h,k%2?'var(--tree2)':'var(--tree)',p+'sway',-k*.7,true));
 b+=flakes+kestrel(330,30,.85);
 return frame(id,css,'',b);
}

/* 2. CANYON */
function sceneCanyon(){
 const id='canyon',p='sc-canyon-';
 /* river centerline (Catmull-Rom sampled) with a width that tapers into the distance */
 const pts=[[536,114,56],[500,106,38],[462,98,26],[424,91,17],[398,82,11],[382,73,7],[394,65,4.5],[388,58,2]];
 const cr=(a,b,c,d,t)=>.5*((2*b)+(-a+c)*t+(2*a-5*b+4*c-d)*t*t+(-a+3*b-3*c+d)*t*t*t);
 const cl=[];
 for(let k=0;k<pts.length-1;k++){const P=n=>pts[Math.max(0,Math.min(pts.length-1,n))];
  for(let u=0;u<8;u++){const t=u/8;cl.push([0,1,2].map(c=>cr(P(k-1)[c],P(k)[c],P(k+1)[c],P(k+2)[c],t)));}}
 cl.push(pts[pts.length-1].slice());
 const Lp=[],Rp=[];
 cl.forEach((q,n)=>{const a=cl[Math.max(0,n-1)],c=cl[Math.min(cl.length-1,n+1)];let dx=c[0]-a[0],dy=c[1]-a[1];const m=Math.hypot(dx,dy)||1;dx/=m;dy/=m;
  const nx=-dy*q[2]/2,ny=dx*q[2]/2;Lp.push([q[0]+nx,q[1]+ny]);Rp.push([q[0]-nx,q[1]-ny]);});
 const pl=a=>a.map((q,n)=>(n?'L':'M')+f1(q[0])+' '+f1(q[1])).join(' ');
 const rv=pl(Lp)+' '+Rp.slice().reverse().map(q=>'L'+f1(q[0])+' '+f1(q[1])).join(' ')+'Z';
 /* glints: small arcs drifting along the flow (toward the viewer, down and to the right) */
 let gl='';
 [[390,62,-4,8],[386,72,-2,8],[398,83,10,6],[428,91,20,5],[466,98,26,6],[506,106,26,7],[446,95,22,5],[488,103,26,6]].forEach(([x,y,dx,dy],k)=>{gl+=`<path class="${p}glint" d="M${x} ${y} q2 -1.8 4 0" fill="none" stroke="var(--snow)" stroke-width="1.3" stroke-linecap="round" style="--dx:${dx}px;--dy:${dy}px;animation-delay:${f1(-k*1.1)}s"/>`;});
 let foam='';
 for(let k=0;k<3;k++)foam+=`<path class="${p}foam" d="M${470+k*14} ${106+(k%2)*2} q3 -2 6 0 t6 0 t6 0" fill="none" stroke="var(--snow)" stroke-width="1.4" stroke-linecap="round" style="animation-delay:${f1(-k*.9)}s"/>`;
 const css=haloCss(id)+`.${p}glint{animation:${p}gl 5.5s ease-in-out infinite;opacity:0}@keyframes ${p}gl{0%{transform:translate(0,0);opacity:0}25%{opacity:1}75%{opacity:1}100%{transform:translate(var(--dx),var(--dy));opacity:0}}
.${p}foam{animation:${p}fm 2.8s ease-in-out infinite alternate}@keyframes ${p}fm{from{opacity:.25;transform:translateX(-3px)}to{opacity:.95;transform:translateX(4px)}}
.${p}orbit{animation:${p}ob 20s linear infinite;transform-origin:0 0}@keyframes ${p}ob{to{transform:rotate(360deg)}}`;
 const defs=`<linearGradient id="${p}river" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="var(--water2)"/><stop offset="1" stop-color="var(--water)"/></linearGradient>`;
 let b=orb(id,372,30);
 /* distant flat rim */
 b+=`<path d="M0 110 L0 56 L214 56 L222 52 L300 52 L306 56 L420 56 L428 51 L560 51 L560 110Z" fill="var(--m1)"/><path d="M0 110 L0 66 L560 66 L560 110Z" fill="var(--m1s)" opacity=".5"/>`;
 /* lower terraced mesa on the left, set back */
 [[206,356,66],[232,334,54],[260,310,42]].forEach(([x1,x2,y])=>{b+=`<rect x="${x1}" y="${y}" width="${x2-x1}" height="${84-y}" fill="var(--sand2)"/><rect x="${x1}" y="${y}" width="${x2-x1}" height="3.5" fill="var(--sand)"/><rect x="${x2-9}" y="${y+3.5}" width="9" height="${82-y}" fill="var(--m3s)" opacity=".45"/>`;});
 b+=`<rect x="206" y="76" width="150" height="2" fill="var(--m2s)" opacity=".45"/><rect x="232" y="62" width="102" height="2" fill="var(--m2s)" opacity=".4"/>`;
 /* tall sheer cliff on the right */
 b+=`<rect x="434" y="22" width="130" height="62" fill="var(--sand2)"/><rect x="434" y="22" width="130" height="5" fill="var(--m1)"/><rect x="434" y="27" width="20" height="57" fill="var(--m3s)" opacity=".55"/>`;
 [[38,5],[54,5],[70,4]].forEach(([y,h])=>{b+=`<rect x="454" y="${y}" width="110" height="${h}" fill="var(--sand)" opacity=".85"/><rect x="454" y="${y+h}" width="110" height="2" fill="var(--m3s)" opacity=".4"/>`;});
 b+=`<rect x="424" y="46" width="12" height="5" fill="var(--sand)"/><rect x="424" y="51" width="12" height="2" fill="var(--m3s)" opacity=".5"/><rect x="428" y="64" width="8" height="4" fill="var(--sand)"/><rect x="428" y="68" width="8" height="2" fill="var(--m3s)" opacity=".5"/>`;
 /* near ground and river */
 b+=`<path d="M0 110 L0 84 L200 84 C300 80 380 84 440 82 C500 81 540 84 560 84 L560 110Z" fill="var(--m3)"/>`;
 b+=`<path d="${rv}" fill="url(#${p}river)"/><path d="${pl(Lp)}" fill="none" stroke="var(--mist)" stroke-width="1.3" opacity=".9"/><path d="${pl(Rp)}" fill="none" stroke="var(--mist)" stroke-width="1.3" opacity=".9"/>`;
 b+=gl+foam;
 b+=`<path d="M150 110 L180 104 C230 100 280 104 322 110Z" fill="var(--m3s)"/>`;
 /* a few slow-circling bird specks above the gorge */
 [[410,44,14,0],[406,38,9,-7]].forEach(([cx,cy,rr,dl])=>{b+=`<g transform="translate(${cx} ${cy})"><g class="${p}orbit" style="animation-delay:${dl}s"><path d="M${rr} 0 q1.5 -1.6 3 0 q1.5 -1.6 3 0" fill="none" stroke="var(--bird)" stroke-width="1.2" stroke-linecap="round"/></g></g>`;});
 b+=kestrel(318,26,.8);
 return frame(id,css,defs,b);
}

/* 3. ISLAND */
function palm(x,y,h,lean,s,k,p){
 const cx=x+lean,cy=y-h;
 let leaves='';
 [[-62,.8],[-28,1],[8,.9],[34,.7]].forEach(([a,l],i)=>{
  [1,-1].forEach(sx=>{leaves+=`<path d="M0 0 Q${f1(9*l)} ${f1(-8*l)} ${f1(21*l)} ${f1(4*l)} Q${f1(10*l)} ${f1(-2*l)} 0 0Z" transform="scale(${sx} 1) rotate(${a})" fill="${(i+(sx>0?0:1))%2?'var(--tree2)':'var(--tree)'}"/>`;});});
 leaves+=`<path d="M0 0 Q-2 -9 1 -14 Q3 -8 0 0Z" fill="var(--tree)"/>`;
 return `<path d="M${f1(x-1.6)} ${y} Q${f1(x+lean*.2)} ${f1(y-h*.55)} ${f1(cx-.8)} ${f1(cy)} L${f1(cx+.8)} ${f1(cy)} Q${f1(x+lean*.2+2)} ${f1(y-h*.55)} ${f1(x+1.6)} ${y}Z" fill="var(--tree)"/><g transform="translate(${f1(cx)} ${f1(cy)}) scale(${s})"><g class="sc-island-frond" style="animation-delay:${-k*1.7}s">${leaves}</g></g>`;
}
function sceneIsland(){
 const id='island',p='sc-island-';
 const css=haloCss(id)+`.${p}frond{animation:${p}sw 5.5s ease-in-out infinite alternate;transform-origin:0 0}@keyframes ${p}sw{from{transform:rotate(-6deg)}to{transform:rotate(6deg)}}
.${p}wave{animation:${p}wv 6s ease-in-out infinite alternate}@keyframes ${p}wv{to{transform:translateX(-40px)}}
.${p}ring{animation:${p}rg 6s ease-out infinite;transform-box:fill-box;transform-origin:center;opacity:0}@keyframes ${p}rg{0%{transform:scale(.9);opacity:0}25%{opacity:.9}100%{transform:scale(1.2);opacity:0}}
.${p}refl{animation:${p}rf 8s ease-in-out infinite alternate;transform-box:fill-box;transform-origin:center}@keyframes ${p}rf{to{transform:scaleX(1.06);opacity:.2}}`;
 const defs=`<linearGradient id="${p}sea" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--water)"/><stop offset="1" stop-color="var(--water2)"/></linearGradient>`;
 let b=orb(id,366,34,12,32);
 b+=`<rect x="0" y="64" width="${W}" height="46" fill="url(#${p}sea)"/>`;
 b+=`<path d="M290 66 C300 60 316 60 328 66Z" fill="var(--m1)"/><path d="M510 66 C522 58 540 56 560 62 L560 66Z" fill="var(--m1)"/>`;
 b+=`<g transform="translate(441 78) scale(1.3) translate(-441 -78)">`;
 b+=`<ellipse class="${p}refl" cx="440" cy="86" rx="46" ry="4" fill="var(--m3)" opacity=".28"/>`;
 for(let k=0;k<3;k++)b+=`<path class="${p}wave" style="animation-delay:${-k*2}s" d="M200 ${74+k*13} q10 -3.5 20 0 t20 0 t20 0 t20 0 t20 0 t20 0 t20 0 t20 0 t20 0 t20 0 t20 0 t20 0 t20 0 t20 0 t20 0 t20 0 t20 0 t20 0" fill="none" stroke="var(--snow)" stroke-width="1.3" opacity="${.5-k*.12}"/>`;
 b+=`<path d="M384 76 C396 70 410 62 430 61 C452 60 472 68 498 76Z" fill="var(--m2)"/><path d="M430 61 C452 60 472 68 498 76 L450 76Z" fill="var(--m2s)"/><path d="M378 77 C396 70 420 69 440 70 C470 70 490 73 504 77 C490 83 462 85 440 85 C414 85 390 83 378 77Z" fill="var(--sand)"/><path d="M440 70 C470 70 490 73 504 77 C490 83 462 85 440 85Z" fill="var(--sand2)" opacity=".75"/>`;
 b+=`<ellipse class="${p}ring" cx="441" cy="80" rx="64" ry="7" fill="none" stroke="var(--snow)" stroke-width="1.2"/><ellipse class="${p}ring" style="animation-delay:-3s" cx="441" cy="80" rx="64" ry="7" fill="none" stroke="var(--snow)" stroke-width="1.2"/>`;
 b+=palm(418,70,23,-4,.85,0)+palm(472,71,20,6,.8,2)+palm(446,70,31,5,1,1);
 b+=`<path d="M404 74 q3 -5 6 0 q-3 -2 -6 0Z M486 75 q3 -4 6 0 q-3 -2 -6 0Z" fill="var(--leaf)"/>`;
 b+=`</g>`;
 b+=kestrel(300,30,.85);
 return frame(id,css,defs,b);
}

/* 4. AURORA */
function sceneAurora(){
 const id='aurora',p='sc-aurora-';
 const r=rng(5);let stars='';
 for(let i=0;i<16;i++)stars+=`<circle cx="${f1(205+r()*355)}" cy="${f1(4+r()*44)}" r="${f1(.5+r()*.7)}" fill="var(--snow)" opacity="${f1(.5+r()*.4)}"/>`;
 const css=haloCss(id)+`.${p}c1{animation:${p}u1 15s ease-in-out infinite alternate;transform-box:fill-box;transform-origin:50% 100%}@keyframes ${p}u1{from{transform:translateX(-26px) scaleY(.8);opacity:.55}to{transform:translateX(26px) scaleY(1.2);opacity:1}}
.${p}c2{animation:${p}u2 21s ease-in-out infinite alternate;transform-box:fill-box;transform-origin:50% 100%}@keyframes ${p}u2{from{transform:translateX(28px) scaleY(1.15);opacity:.45}to{transform:translateX(-28px) scaleY(.8);opacity:1}}
.${p}ray{animation:${p}rr 9s ease-in-out infinite alternate}@keyframes ${p}rr{from{opacity:.1}to{opacity:.9}}
.${p}tw{animation:${p}tw 5s ease-in-out infinite alternate}@keyframes ${p}tw{to{opacity:.45}}
.${p}sway{animation:${p}sw 7s ease-in-out infinite alternate;transform-origin:bottom;transform-box:fill-box}@keyframes ${p}sw{to{transform:skewX(1.2deg)}}`;
 const defs=`<linearGradient id="${p}g1" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--aur1)" stop-opacity="0"/><stop offset=".45" stop-color="var(--aur1)" stop-opacity=".85"/><stop offset="1" stop-color="var(--aur1)" stop-opacity="0"/></linearGradient><linearGradient id="${p}g2" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--aur2)" stop-opacity="0"/><stop offset=".5" stop-color="var(--aur2)" stop-opacity=".8"/><stop offset="1" stop-color="var(--aur2)" stop-opacity="0"/></linearGradient>`;
 let b=`<g class="sc-night ${p}tw">${stars}</g>`;
 b+=orb(id,452,40,10,28);
 /* ribbons: base strength is faint dawn, night layer adds full strength */
 const rb1=`<path d="M60 62 L200 56 C240 30 290 52 330 30 S410 22 460 34 S530 28 570 24 L570 46 C530 54 490 48 450 58 S370 48 330 62 S240 78 200 74 L60 80Z" fill="url(#${p}g1)"/>`;
 const rb2=`<path d="M100 70 L250 66 C290 44 330 62 372 40 S450 36 500 46 S548 42 570 44 L570 62 C520 66 470 62 430 72 S350 64 310 78 S270 84 250 82 L100 86Z" fill="url(#${p}g2)"/>`;
 b+=`<g opacity=".8"><g class="${p}c1">${rb1}</g><g class="${p}c2">${rb2}</g></g>`;
 b+=`<g class="sc-night"><g class="${p}c1" style="animation-delay:-4s"><g opacity=".6">${rb1}</g></g><g class="${p}c2" style="animation-delay:-8s"><g opacity=".55">${rb2}</g></g>`;
 [[330,30,50],[372,40,44],[430,36,48],[480,36,44],[520,30,46]].forEach(([x,y,h],k)=>b+=`<rect class="${p}ray" style="animation-delay:${-k*1.8}s" x="${x}" y="${y}" width="1.4" height="${h}" fill="var(--aur1)" opacity=".4"/>`);
 b+=`</g>`;
 b+=`<path d="M0 110 L0 78 L200 78 L232 66 L262 74 L300 56 L330 70 L372 60 L410 72 L444 58 L480 70 L520 56 L560 64 L560 110Z" fill="var(--m2)"/><path d="M300 56 L330 70 L372 60 L372 110 L300 110Z M444 58 L480 70 L520 56 L520 110 L444 110Z" fill="var(--m2s)" opacity=".55"/>`;
 b+=`<path d="M0 110 L0 94 L200 94 C260 84 310 90 360 86 C420 82 470 90 520 84 L560 86 L560 110Z" fill="var(--m3)"/>`;
 [[300,98,22],[314,100,30],[330,98,18],[470,98,26],[486,101,36],[504,99,24],[526,100,32],[546,98,22]].forEach(([x,y,h],k)=>{
  const w=h*.34;b+=`<path class="${p}sway" style="animation-delay:${-k*.9}s" d="M${x} ${y-h} L${f1(x+w)} ${f1(y-h*.55)} L${f1(x+w*.5)} ${f1(y-h*.55)} L${f1(x+w*1.2)} ${f1(y-h*.2)} L${f1(x+w*.6)} ${f1(y-h*.2)} L${f1(x+w*1.35)} ${y} L${f1(x-w*1.35)} ${y} L${f1(x-w*.6)} ${f1(y-h*.2)} L${f1(x-w*1.2)} ${f1(y-h*.2)} L${f1(x-w*.5)} ${f1(y-h*.55)} L${f1(x-w)} ${f1(y-h*.55)}Z" fill="${k%2?'var(--tree2)':'var(--tree)'}"/>`;});
 b+=kestrel(350,22,.8);
 return frame(id,css,defs,b);
}

/* 5. RAIN */
function sceneRain(){
 const id='rain',p='sc-rain-';
 const r=rng(23);let drops='';
 for(let i=0;i<24;i++){const x=352+r()*110,y=r()*30,dur=1.3+r()*.9,dl=-r()*dur;
  drops+=`<g transform="translate(0 ${f1(y)})"><path class="${p}drop" d="M${f1(x)} 54 l-2.6 7" stroke="var(--m3s)" stroke-width="1.2" stroke-linecap="round" fill="none" style="animation-duration:${f1(dur)}s;animation-delay:${f1(dl)}s"/></g>`;}
 const css=haloCss(id)+`.${p}drop{animation:${p}fall 2s linear infinite;opacity:0}
@keyframes ${p}fall{0%{transform:translate(0,0);opacity:0}15%{opacity:.9}85%{opacity:.9}100%{transform:translate(-14px,36px);opacity:0}}
.${p}bow{animation:${p}bow 22s ease-in-out infinite}@keyframes ${p}bow{0%,10%{opacity:.15}40%,62%{opacity:1}90%,100%{opacity:.15}}
.${p}rip{animation:${p}rip 3.2s ease-out infinite;transform-box:fill-box;transform-origin:center;opacity:0}@keyframes ${p}rip{0%{transform:scale(.3);opacity:0}20%{opacity:.8}100%{transform:scale(1.2);opacity:0}}
.${p}cloud{animation:${p}cd 18s ease-in-out infinite alternate}@keyframes ${p}cd{to{transform:translateX(-32px)}}`;
 let b=orb(id,458,32,11,30);
 /* rainbow, day only */
 const cols=['var(--bad)','var(--lite)','var(--aur1)','var(--water2)','var(--aur2)'];
 b+=`<g class="sc-day"><g class="${p}bow" fill="none" stroke-width="1.6" opacity=".6">${cols.map((c,i)=>`<path d="M${428+i*1.6} 98 A${72-i*1.6} ${60-i*1.4} 0 0 1 ${572-i*1.6} 98" stroke="${c}"/>`).join('')}</g></g>`;
 b+=`<path d="M0 110 L0 84 L190 84 C230 74 270 64 320 70 C370 76 400 62 450 64 C500 66 530 58 560 62 L560 110Z" fill="var(--m1)"/>`;
 b+=`<path d="M320 70 C370 76 400 62 450 64 C500 66 530 58 560 62 L560 76 C520 74 490 78 450 76 C410 74 370 86 320 70Z" fill="var(--m1s)" opacity=".6"/>`;
 b+=`<path d="M0 110 L0 96 L190 96 C240 84 290 82 340 88 C400 94 440 80 500 82 C530 83 548 80 560 80 L560 110Z" fill="var(--m2)"/><path d="M340 88 C400 94 440 80 500 82 C530 83 548 80 560 80 L560 90 C520 90 480 98 430 96 C390 96 360 98 340 88Z" fill="var(--m2s)" opacity=".5"/>`;
 b+=`<path d="M0 110 L0 102 L190 102 C240 102 300 98 360 102 C420 106 470 98 530 100 L560 99 L560 110Z" fill="var(--m3)"/>`;
 b+=`<ellipse cx="402" cy="104" rx="14" ry="2.6" fill="var(--water)" opacity=".7"/><ellipse class="${p}rip" cx="402" cy="104" rx="12" ry="2.2" fill="none" stroke="var(--snow)" stroke-width="1"/><ellipse class="${p}rip" style="animation-delay:-1.6s" cx="402" cy="104" rx="12" ry="2.2" fill="none" stroke="var(--snow)" stroke-width="1"/>`;
 [[266,92,12],[280,93,16],[516,90,14],[534,92,18]].forEach(([x,y,h],k)=>{b+=`<path d="M${x} ${y} L${x} ${y-h*.4}" stroke="var(--tree)" stroke-width="1.4"/><ellipse cx="${x}" cy="${y-h*.7}" rx="${h*.35}" ry="${h*.45}" fill="${k%2?'var(--tree2)':'var(--tree)'}"/>`;});
 b+=drops;
 /* raincloud */
 b+=`<g class="${p}cloud"><g transform="translate(58 0)"><path d="M286 42 C280 30 296 22 308 27 C310 14 332 9 344 19 C354 10 376 13 380 27 C394 22 408 30 402 42 C408 48 402 52 394 52 L294 52 C286 52 282 46 286 42Z" fill="var(--m2)"/><path d="M294 52 L394 52 C402 52 408 48 402 42 C394 46 340 47 290 44 C282 46 286 52 294 52Z" fill="var(--m2s)"/><path d="M308 27 C310 14 332 9 344 19 C354 10 376 13 380 27 C360 21 330 20 308 27Z" fill="var(--mist)" opacity=".4"/></g></g>`;
 b+=kestrel(250,34,.85);
 return frame(id,css,'',b);
}

/* 6. SAVANNA */
function acacia(x,y,h,w,fill,fill2){
 const q=(x1,y1,x2,y2,t)=>`M${f1(x1-t)} ${f1(y1)} L${f1(x2-t*.5)} ${f1(y2)} L${f1(x2+t*.5)} ${f1(y2)} L${f1(x1+t)} ${f1(y1)}Z`;
 const fy=y-h*.5,cy=y-h*.82,t=Math.max(1,w*.07);
 let d=`M${f1(x-t*1.4)} ${y} L${f1(x-t*.7)} ${f1(fy)} L${f1(x+t*.7)} ${f1(fy)} L${f1(x+t*1.4)} ${y}Z`;
 d+=q(x,fy+1,x-w*.5,cy+2,t*.6)+q(x,fy+1,x+w*.5,cy+2,t*.6)+q(x,fy+1,x-w*.1,cy-1,t*.5)+q(x-w*.28,fy-h*.12,x-w*.7,cy+1,t*.4)+q(x+w*.28,fy-h*.12,x+w*.7,cy+1,t*.4);
 const tier=(cx,cyy,ww,tt)=>`M${f1(cx-ww)} ${f1(cyy)} C${f1(cx-ww*.9)} ${f1(cyy-tt)} ${f1(cx-ww*.4)} ${f1(cyy-tt*1.25)} ${f1(cx)} ${f1(cyy-tt*1.1)} C${f1(cx+ww*.4)} ${f1(cyy-tt*1.3)} ${f1(cx+ww*.9)} ${f1(cyy-tt)} ${f1(cx+ww)} ${f1(cyy)} C${f1(cx+ww*.5)} ${f1(cyy+tt*.3)} ${f1(cx-ww*.5)} ${f1(cyy+tt*.3)} ${f1(cx-ww)} ${f1(cyy)}Z`;
 const th=h*.17;
 return `<path d="${d}" fill="${fill}"/><path d="${tier(x-w*.08,cy+th*.3,w,th)}" fill="${fill}"/><path d="${tier(x+w*.18,cy-th*.75,w*.62,th*.8)}" fill="${fill2}"/><path d="${tier(x-w*.5,cy-th*.1,w*.4,th*.6)}" fill="${fill2}" opacity=".8"/>`;
}
function giraffe(s){
 return `<g transform="scale(${s})"><path d="M-4.2 -8 h1 v7.6 h-1Z M-2.2 -8 h1 v7.6 h-1Z M1.6 -8 h1 v7.6 h-1Z M3.6 -8 h1 v7.6 h-1Z" fill="var(--m3s)"/><ellipse cx="0" cy="-9.2" rx="5.4" ry="2.7" fill="var(--m3s)"/><path d="M3 -10.5 L5.6 -19.6 L7.4 -19 L5.6 -9Z" fill="var(--m3s)"/><path d="M6.2 -19.6 L9.8 -19.8 L9.9 -18.4 L6.6 -18Z" fill="var(--m3s)"/><path d="M-5.2 -10 q-1.6 2 -1.2 5" fill="none" stroke="var(--m3s)" stroke-width="1"/></g>`;
}
function sceneSavanna(){
 const id='savanna',p='sc-savanna-';
 const css=haloCss(id)+`.${p}grass{animation:${p}gs 3s ease-in-out infinite alternate;transform-origin:bottom;transform-box:fill-box}@keyframes ${p}gs{from{transform:skewX(-10deg)}to{transform:skewX(11deg)}}
.${p}haze{animation:${p}hz 9s ease-in-out infinite alternate}@keyframes ${p}hz{from{opacity:.2}to{opacity:.7}}
.${p}hill{animation:${p}hl 7s ease-in-out infinite alternate;transform-box:fill-box;transform-origin:50% 100%}@keyframes ${p}hl{to{transform:scaleY(1.015)}}
.${p}walk{animation:${p}wk 70s linear infinite}@keyframes ${p}wk{0%{transform:translateX(-40px);opacity:0}8%{opacity:1}92%{opacity:1}100%{transform:translateX(70px);opacity:0}}
.${p}step{animation:${p}st 1.8s ease-in-out infinite alternate}@keyframes ${p}st{to{transform:translateY(-.7px)}}`;
 const defs=`<linearGradient id="${p}hz" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--mist)" stop-opacity="0"/><stop offset=".55" stop-color="var(--mist)" stop-opacity=".5"/><stop offset="1" stop-color="var(--mist)" stop-opacity="0"/></linearGradient>`;
 let b=orb(id,420,38,12,32);
 /* distant hill (gently wavering) and ridge */
 b+=`<g class="${p}hill"><path d="M0 110 L0 74 L200 74 C250 70 290 56 332 44 C356 38 378 44 404 56 C436 66 480 66 520 62 L560 62 L560 110Z" fill="var(--m1)"/><path d="M332 44 C356 38 378 44 404 56 C392 62 374 72 352 80 C346 66 340 54 332 44Z" fill="var(--m1s)" opacity=".5"/></g>`;
 b+=`<path d="M0 110 L0 80 L260 78 C320 72 380 70 440 74 C490 76 530 70 560 70 L560 110Z" fill="var(--m2)"/><path d="M0 110 L0 86 L260 84 C340 82 420 82 560 80 L560 110Z" fill="var(--m2s)" opacity=".4"/>`;
 /* soft heat haze: a wide band that breathes, no lines */
 b+=`<rect class="${p}haze" x="0" y="60" width="560" height="30" fill="url(#${p}hz)"/>`;
 b+=acacia(468,80,14,14,'var(--tree3)','var(--tree3)')+acacia(260,80,12,13,'var(--tree3)','var(--tree3)');
 /* a small herd walking slowly across the plain */
 [[290,89,.9,0],[330,90,1,-4],[364,89,.8,-9],[404,90,.95,-14]].forEach(([x,y,sc,d],k)=>{b+=`<g transform="translate(${x} ${y})"><g class="${p}walk" style="animation-delay:${d*-1.5}s"><g class="${p}step" style="animation-delay:${-k*.5}s">${giraffe(sc)}</g></g></g>`;});
 b+=`<path d="M0 110 L0 96 L200 94 C260 90 340 94 420 92 C480 90 520 94 560 92 L560 110Z" fill="var(--m3)"/>`;
 b+=acacia(372,98,20,20,'var(--tree2)','var(--tree3)')+acacia(436,100,28,28,'var(--tree)','var(--tree2)')+acacia(512,108,52,44,'var(--tree)','var(--tree2)');
 /* foreground grass: a wind ripple travels left to right via staggered delays */
 const r=rng(9);
 const bl=[];for(let i=0;i<40;i++)bl.push([215+r()*340,8+r()*11,i%3?'var(--tree2)':'var(--tree)',107+r()*4]);
 bl.forEach(([x,h,c,y])=>{const fr=(x-200)/360;
  b+=`<path class="${p}grass" style="animation-delay:${f1(-fr*4.8)}s" d="M${f1(x-1.6)} ${f1(y)} Q${f1(x-.5)} ${f1(y-h*.6)} ${f1(x+1)} ${f1(y-h)} Q${f1(x+1.3)} ${f1(y-h*.5)} ${f1(x+1.8)} ${f1(y)}Z" fill="${c}"/>`;});
 b+=kestrel(318,30,.85);
 return frame(id,css,defs,b);
}

SCENES.push({id:'winter',name:'Winter',moves:'Snow falls gently, smoke drifts from the cabin chimney',draw:sceneWinter});
SCENES.push({id:'canyon',name:'Canyon',moves:'River flow lines move slowly through the gorge',draw:sceneCanyon});
SCENES.push({id:'island',name:'Island',moves:'Palm fronds sway, small waves ring the island',draw:sceneIsland});
SCENES.push({id:'aurora',name:'Aurora',moves:'Aurora ribbons slowly undulate',draw:sceneAurora});
SCENES.push({id:'rain',name:'Rain',moves:'Light rain falls, a rainbow fades in and out, a puddle ripples',draw:sceneRain});
SCENES.push({id:'savanna',name:'Savanna',moves:'Wind ripples through the grass, giraffes walk, haze breathes',draw:sceneSavanna});
})();
