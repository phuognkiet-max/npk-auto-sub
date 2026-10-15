/* Source-pixel geometry shared by direct manipulation and the inspector. */
"use strict";
(() => {
  const limit = (v, low, high) => Math.max(low, Math.min(high, v));
  function clamp(r, frame) {
    const width = Math.max(1, Number(frame.width)), height = Math.max(1, Number(frame.height));
    const x = limit(Number(r.x), 0, width - 1), y = limit(Number(r.y), 0, height - 1);
    return {...r, x, y, w: limit(Number(r.w), 1, width - x), h: limit(Number(r.h), 1, height - y)};
  }
  function move(rows, dx, dy, frame) {
    const left = Math.min(...rows.map(r => r.x)), top = Math.min(...rows.map(r => r.y));
    const right = Math.max(...rows.map(r => r.x+r.w)), bottom = Math.max(...rows.map(r => r.y+r.h));
    dx = limit(dx, -left, frame.width-right); dy = limit(dy, -top, frame.height-bottom);
    return rows.map(r => ({...r, x: r.x+dx, y: r.y+dy}));
  }
  function resize(r, handle, dx, dy, frame, aspect = false) {
    let left=r.x, top=r.y, right=r.x+r.w, bottom=r.y+r.h;
    if (handle.includes('w')) left=limit(left+dx,0,right-1);
    if (handle.includes('e')) right=limit(right+dx,left+1,frame.width);
    if (handle.includes('n')) top=limit(top+dy,0,bottom-1);
    if (handle.includes('s')) bottom=limit(bottom+dy,top+1,frame.height);
    if (aspect && handle.length===2) {
      const ratio=r.w/r.h, anchorX=handle.includes('w')?r.x+r.w:r.x, anchorY=handle.includes('n')?r.y+r.h:r.y;
      let w=right-left, h=bottom-top;
      if (Math.abs(dx)/r.w >= Math.abs(dy)/r.h) h=w/ratio; else w=h*ratio;
      w=Math.max(w,1,ratio); h=w/ratio;
      const maxW=handle.includes('w')?anchorX:frame.width-anchorX;
      const maxH=handle.includes('n')?anchorY:frame.height-anchorY;
      const scale=Math.min(1,maxW/w,maxH/h); w*=scale; h*=scale;
      if (w>=1 && h>=1) {
        left=handle.includes('w')?anchorX-w:anchorX; right=left+w;
        top=handle.includes('n')?anchorY-h:anchorY; bottom=top+h;
      }
    }
    return clamp({...r,x:left,y:top,w:right-left,h:bottom-top},frame);
  }
  function union(rows) {
    const x=Math.min(...rows.map(r=>r.x)), y=Math.min(...rows.map(r=>r.y));
    return {x,y,w:Math.max(...rows.map(r=>r.x+r.w))-x,h:Math.max(...rows.map(r=>r.y+r.h))-y,
      start:Math.min(...rows.map(r=>r.start)),end:Math.max(...rows.map(r=>r.end))};
  }
  globalThis.NpkRegionGeometry = Object.freeze({clamp,move,resize,union});
})();
