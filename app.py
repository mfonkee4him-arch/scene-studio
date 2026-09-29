"""Scene Studio: audio (or script) + a style sample -> timed still images that play with the audio.

Setup:  pip install -r requirements.txt
Run:    export GEMINI_API_KEY=your_key   (Windows: set GEMINI_API_KEY=your_key)
        python app.py   then open http://localhost:5000

To use another image service, add a function to PROVIDERS and set IMAGE_PROVIDER.
"""
import os, json, uuid, time, threading, mimetypes
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, Response
from google import genai
from google.genai import types

DATA = Path("projects")
DATA.mkdir(exist_ok=True)
TEXT_MODEL = os.getenv("TEXT_MODEL", "gemini-2.5-flash")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gemini-2.5-flash-image")
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
app = Flask(__name__)
lock = threading.Lock()


@app.before_request
def guard():
    pw = os.getenv("APP_PASSWORD")
    if pw and (not request.authorization or request.authorization.password != pw):
        return Response("Password required", 401, {"WWW-Authenticate": 'Basic realm="Scene Studio"'})


# ---------- image providers (swap here) ----------
def gemini_image(style_bytes, style_mime, prompt):
    r = client.models.generate_content(
        model=IMAGE_MODEL,
        contents=[types.Part.from_bytes(data=style_bytes, mime_type=style_mime), prompt],
    )
    for part in r.candidates[0].content.parts:
        if part.inline_data:
            return part.inline_data.data
    raise RuntimeError("The model returned no image")


PROVIDERS = {"gemini": gemini_image}
generate_image = PROVIDERS[os.getenv("IMAGE_PROVIDER", "gemini")]


# ---------- storage ----------
def pdir(pid): return DATA / pid
def load(pid): return json.loads((pdir(pid) / "project.json").read_text())
def save(pid, p): (pdir(pid) / "project.json").write_text(json.dumps(p))


def styled(prompt):
    return ("Create one single still image. Copy the art style, color palette, lighting, "
            "and level of detail of the reference image exactly, but show this new scene: "
            + prompt + ". Do not include any text or captions in the image.")


# ---------- pipeline ----------
def split_scenes(audio_path, script, secs):
    f = client.files.upload(file=str(audio_path))
    ask = (f"Listen to this audio and split it into scenes of roughly {secs} seconds, cutting at natural "
           'pauses. Return a JSON list like [{"start":0.0,"end":6.5,"text":"what is said",'
           '"prompt":"a vivid description of one still image that illustrates it"}]. '
           "Times are in seconds. Scenes must cover the whole audio with no gaps.")
    if script:
        ask += "\nScript for reference:\n" + script
    r = client.models.generate_content(
        model=TEXT_MODEL, contents=[f, ask],
        config=types.GenerateContentConfig(response_mime_type="application/json"))
    return json.loads(r.text)


def make_image(pid, i):
    p = load(pid)
    style = (pdir(pid) / p["style_file"]).read_bytes()
    prompt = p["scenes"][i]["prompt"]
    name, err = None, None
    for attempt in range(3):
        try:
            img = generate_image(style, p["style_mime"], styled(prompt))
            name = f"scene_{i}_{uuid.uuid4().hex[:6]}.png"
            (pdir(pid) / name).write_bytes(img)
            break
        except Exception as e:
            err = str(e)
            time.sleep(20 if "429" in err else 3)
    with lock:
        p = load(pid)
        sc = p["scenes"][i]
        sc["busy"] = False
        if name:
            sc["image"], sc["error"] = name, None
        else:
            sc["error"] = err
        save(pid, p)


def run(pid):
    try:
        p = load(pid)
        scenes = split_scenes(pdir(pid) / p["audio_file"], p["script"], p["secs"])
        with lock:
            p = load(pid)
            p["scenes"] = [dict(s, image=None, error=None, busy=True) for s in scenes]
            p["status"] = "Generating images"
            save(pid, p)
        for i in range(len(scenes)):
            make_image(pid, i)
        with lock:
            p = load(pid)
            p["status"] = "done"
            save(pid, p)
    except Exception as e:
        with lock:
            p = load(pid)
            p["status"] = "error: " + str(e)
            save(pid, p)


# ---------- routes ----------
@app.post("/api/create")
def create():
    pid = uuid.uuid4().hex[:8]
    pdir(pid).mkdir()
    a, s = request.files["audio"], request.files["style"]
    af = "audio" + Path(a.filename).suffix
    sf = "style" + Path(s.filename).suffix
    a.save(pdir(pid) / af)
    s.save(pdir(pid) / sf)
    save(pid, {"audio_file": af, "style_file": sf,
               "style_mime": mimetypes.guess_type(sf)[0] or "image/png",
               "script": request.form.get("script", ""),
               "secs": int(request.form.get("secs") or 6),
               "status": "Splitting audio into scenes", "scenes": []})
    threading.Thread(target=run, args=(pid,), daemon=True).start()
    return jsonify(id=pid)


@app.get("/api/p/<pid>")
def get_project(pid): return jsonify(load(pid))


@app.get("/f/<pid>/<name>")
def files(pid, name): return send_from_directory(pdir(pid).resolve(), name)


@app.post("/api/p/<pid>/scene/<int:i>")
def regen(pid, i):
    with lock:
        p = load(pid)
        p["scenes"][i]["prompt"] = request.json["prompt"]
        p["scenes"][i]["busy"] = True
        save(pid, p)
    threading.Thread(target=make_image, args=(pid, i), daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/p/<pid>/scene/<int:i>/upload")
def upload(pid, i):
    f = request.files["image"]
    name = f"scene_{i}_{uuid.uuid4().hex[:6]}{Path(f.filename).suffix}"
    f.save(pdir(pid) / name)
    with lock:
        p = load(pid)
        p["scenes"][i].update(image=name, error=None)
        save(pid, p)
    return jsonify(ok=True)


@app.get("/")
def index(): return Response(PAGE, mimetype="text/html")


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Scene Studio</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;background:#111;color:#eee}
main{max-width:900px;margin:auto;padding:16px}
input,textarea,button,.btn{font:inherit;border-radius:8px;border:1px solid #444;background:#1c1c1c;color:#eee;padding:8px}
button,.btn{cursor:pointer;background:#2d5bd6;border:0;display:inline-block}
label{display:block;margin:10px 0 4px;color:#aaa;font-size:14px}
textarea{width:100%;box-sizing:border-box;min-height:70px}
#stage{width:100%;aspect-ratio:16/9;object-fit:contain;background:#000;border-radius:10px}
audio{width:100%;margin:8px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.card{background:#1a1a1a;padding:10px;border-radius:10px}
.card img{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:6px;background:#000}
.err{color:#f77;font-size:13px}.t{font-size:12px;color:#999;margin:4px 0}
</style></head><body><main>
<h2>Scene Studio</h2>
<form id="f" onsubmit="create(event)">
<label>Voice or audio file</label><input type="file" name="audio" accept="audio/*" required>
<label>Style sample image</label><input type="file" name="style" accept="image/*" required>
<label>Script (optional, improves accuracy)</label><textarea name="script"></textarea>
<label>Seconds per scene (approx)</label><input type="number" name="secs" value="6" min="2" max="30">
<p><button>Generate</button></p></form>
<p id="status"></p>
<div id="player" hidden><img id="stage"><audio id="au" controls></audio></div>
<div class="grid" id="scenes"></div>
</main>
<script>
let pid=location.hash.slice(1),P=null,timer,cur=null;
const $=s=>document.querySelector(s);
async function create(e){e.preventDefault();
 const r=await fetch('/api/create',{method:'POST',body:new FormData($('#f'))});
 pid=(await r.json()).id;location.hash=pid;$('#scenes').innerHTML='';$('#au').removeAttribute('src');poll();}
async function poll(){clearTimeout(timer);if(!pid)return;
 P=await (await fetch('/api/p/'+pid)).json();render();
 if(P.status!=='done'&&!P.status.startsWith('error')||P.scenes.some(s=>s.busy))timer=setTimeout(poll,2000);}
function fmt(s){return s.start.toFixed(1)+'s to '+s.end.toFixed(1)+'s'}
function render(){
 $('#status').textContent=P.status==='done'?'':P.status;
 $('#player').hidden=!P.scenes.length;
 const au=$('#au');if(!au.getAttribute('src'))au.src='/f/'+pid+'/'+P.audio_file;
 const box=$('#scenes');
 if(box.children.length!==P.scenes.length){box.innerHTML='';
  P.scenes.forEach((s,i)=>{const d=document.createElement('div');d.className='card';
   d.innerHTML='<img><div class="t"></div><textarea></textarea><p><button>Regenerate</button> <label class="btn" style="margin:0">Upload<input type="file" accept="image/*" hidden></label></p><div class="err"></div>';
   d.querySelector('textarea').value=s.prompt;
   d.querySelector('button').onclick=()=>regen(i,d);
   d.querySelector('input').onchange=e=>up(i,e.target);
   box.appendChild(d);});}
 P.scenes.forEach((s,i)=>{const d=box.children[i];
  d.querySelector('.t').textContent='Scene '+(i+1)+', '+fmt(s)+(s.busy?' (working...)':'');
  if(s.image)d.querySelector('img').src='/f/'+pid+'/'+s.image;
  d.querySelector('.err').textContent=s.error||'';});
 show(au.currentTime);}
async function regen(i,d){await fetch(`/api/p/${pid}/scene/${i}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:d.querySelector('textarea').value})});poll();}
async function up(i,inp){const fd=new FormData();fd.append('image',inp.files[0]);await fetch(`/api/p/${pid}/scene/${i}/upload`,{method:'POST',body:fd});poll();}
function show(t){if(!P)return;const s=P.scenes.find(s=>t>=s.start&&t<s.end)||P.scenes.find(s=>s.image);
 if(s&&s.image&&s.image!==cur){cur=s.image;$('#stage').src='/f/'+pid+'/'+s.image;}}
$('#au').ontimeupdate=e=>show(e.target.currentTime);
if(pid)poll();
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
