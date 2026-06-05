import asyncio
import json
import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

try:
    from pynput import keyboard
except Exception:
    keyboard = None

try:
    from playwright.async_api import async_playwright
except Exception:
    async_playwright = None


APP_DIR = Path(__file__).resolve().parent
PROFILES_FILE = APP_DIR / "profiles.json"
DEFAULT_URL = "https://www.google.com"
BROWSER_CHOICES = ("Brave", "Firefox", "Chrome")


def find_brave_executable():
    for candidate in (shutil.which("brave"), shutil.which("brave.exe"), shutil.which("brave-browser")):
        if candidate:
            return candidate

    candidates = []
    if os.name == "nt":
        for env_name in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(Path(base) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe")
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"))
    else:
        candidates.extend(
            [
                Path("/usr/bin/brave-browser"),
                Path("/usr/bin/brave"),
                Path("/snap/bin/brave"),
                Path("/opt/brave.com/brave/brave"),
            ]
        )

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return None


def find_chrome_executable():
    for candidate in (shutil.which("chrome"), shutil.which("chrome.exe"), shutil.which("google-chrome"), shutil.which("google-chrome-stable")):
        if candidate:
            return candidate

    candidates = []
    if os.name == "nt":
        for env_name in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    else:
        candidates.extend(
            [
                Path("/usr/bin/google-chrome"),
                Path("/usr/bin/google-chrome-stable"),
                Path("/opt/google/chrome/google-chrome"),
            ]
        )

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return None


def default_browser_choice():
    return "Brave" if find_brave_executable() else "Firefox"


def normalize_browser_choice(value):
    choice = str(value or "").strip().lower()
    if choice in {"firefox", "mozilla firefox"}:
        return "Firefox"
    if choice in {"brave", "brave browser"}:
        return "Brave"
    if choice in {"chrome", "google chrome"}:
        return "Chrome"
    return default_browser_choice()

AD_BLOCK_PATTERNS = (
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "google-analytics.com",
    "googletagmanager.com",
    "googletagservices.com",
    "adservice.google.",
    "adsystem.com",
    "adnxs.com",
    "ads-twitter.com",
    "amazon-adsystem.com",
    "analytics.tiktok.com",
    "appsflyer.com",
    "bingads.microsoft.com",
    "criteo.com",
    "facebook.net",
    "fbcdn.net/ads",
    "hotjar.com",
    "outbrain.com",
    "scorecardresearch.com",
    "taboola.com",
    "tracking",
    "/ads?",
    "/ads/",
    "/adserver/",
    "/advertising/",
    "/banner_ad",
)


@dataclass
class ClickStep:
    selector: str = ""
    x: float = 0.0
    y: float = 0.0
    delay: float = 0.0

    @classmethod
    def from_dict(cls, data):
        return cls(
            selector=str(data.get("selector", "")),
            x=float(data.get("x", 0.0)),
            y=float(data.get("y", 0.0)),
            delay=float(data.get("delay", 0.0)),
        )

    def to_dict(self):
        return {
            "selector": self.selector,
            "x": self.x,
            "y": self.y,
            "delay": self.delay,
        }


@dataclass
class Profile:
    profile_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "Perfil 1"
    url: str = DEFAULT_URL
    browser: str = field(default_factory=default_browser_choice)
    incognito: bool = False
    steps: list[ClickStep] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data):
        return cls(
            profile_id=str(data.get("profile_id") or uuid.uuid4()),
            name=str(data.get("name") or "Perfil"),
            url=str(data.get("url") or DEFAULT_URL),
            browser=normalize_browser_choice(data.get("browser")),
            incognito=bool(data.get("incognito", False)),
            steps=[ClickStep.from_dict(item) for item in data.get("steps", [])],
        )

    def to_dict(self):
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "url": self.url,
            "browser": normalize_browser_choice(self.browser),
            "incognito": self.incognito,
            "steps": [step.to_dict() for step in self.steps],
        }


class ProfileStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self):
        if not self.path.exists():
            return [Profile()]
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            profiles = [Profile.from_dict(item) for item in data.get("profiles", [])]
            return profiles or [Profile()]
        except Exception:
            return [Profile()]

    def save(self, profiles):
        payload = {"profiles": [profile.to_dict() for profile in profiles]}
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


class BrowserSession:
    def __init__(self, on_click, on_status, on_error, on_stopped):
        self.on_click = on_click
        self.on_status = on_status
        self.on_error = on_error
        self.on_stopped = on_stopped
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.playwright = None
        self.browser = None
        self.browser_name = None
        self.browser_incognito = None
        self.context = None
        self.page = None
        self.context_incognito = False
        self.record_binding_added = False
        self.adblock_installed = False
        self.run_task = None
        self.stop_event = None

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _submit(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        future.add_done_callback(self._report_future_error)
        return future

    def _report_future_error(self, future):
        try:
            future.result()
        except Exception as exc:
            self.on_error(str(exc))

    def open_browser(self, url, browser_name=None, incognito=False):
        self._submit(self._open_browser(url, browser_name, incognito))

    async def _open_browser(self, url, browser_name=None, incognito=False):
        if async_playwright is None:
            raise RuntimeError("Playwright nao esta instalado. Rode: pip install -r requirements.txt")
        if self.playwright is None:
            self.playwright = await async_playwright().start()

        browser_name = normalize_browser_choice(browser_name)

        if self.browser is not None and self.browser_name != browser_name:
            pending_browser = await self._launch_browser(browser_name, incognito)
            await self._close_browser_only()
            self.browser = pending_browser
            self.browser_name = browser_name
            self.browser_incognito = incognito
        elif self.browser is not None and self.browser_incognito != incognito:
            await self._close_browser_only()

        if self.browser is None:
            self.browser = await self._launch_browser(browser_name, incognito)
            self.browser_name = browser_name
            self.browser_incognito = incognito

        if self.context is None or self.context_incognito != incognito or incognito:
            if self.context:
                await self.context.close()
            self.context = await self.browser.new_context(no_viewport=True)
            self.context_incognito = incognito
            self.page = None
            self.record_binding_added = False
            self.adblock_installed = False
            await self._install_adblock()

        if self.page is None or self.page.is_closed():
            self.page = await self.context.new_page()
            self.record_binding_added = False

        target = (url or DEFAULT_URL).strip()
        if not target.startswith(("http://", "https://")):
            target = "https://" + target
        self.on_status("Abrindo pagina...")
        await self.page.goto(target, wait_until="domcontentloaded")
        mode = "anonimo" if incognito else "normal"
        self.on_status(f"Navegador {browser_name.lower()} aberto em modo {mode} com adblock")

    async def _launch_browser(self, browser_name, incognito):
        if browser_name == "Firefox":
            launch_options = {"headless": False}
            if incognito:
                launch_options["args"] = ["-private-window"]
            try:
                return await self.playwright.firefox.launch(**launch_options)
            except Exception as exc:
                raise RuntimeError("Firefox do Playwright nao esta instalado. Rode: python -m playwright install firefox") from exc

        if browser_name == "Chrome":
            chrome_executable = find_chrome_executable()
            launch_options = {"headless": False}
            if incognito:
                launch_options["args"] = ["--incognito"]
            if chrome_executable:
                launch_options["executable_path"] = chrome_executable
            else:
                launch_options["channel"] = "chrome"
            try:
                return await self.playwright.chromium.launch(**launch_options)
            except Exception as exc:
                raise RuntimeError("Chrome nao encontrado. Instale o Google Chrome ou escolha Brave/Firefox.") from exc

        brave_executable = find_brave_executable()
        if not brave_executable:
            raise RuntimeError("Brave nao encontrado. Instale o Brave ou escolha Firefox.")

        launch_options = {"headless": False, "executable_path": brave_executable}
        if incognito:
            launch_options["args"] = ["--incognito"]
        return await self.playwright.chromium.launch(**launch_options)

    async def _install_adblock(self):
        if self.adblock_installed or self.context is None:
            return

        async def route_handler(route):
            request = route.request
            url = request.url.lower()
            resource_type = request.resource_type
            should_block = any(pattern in url for pattern in AD_BLOCK_PATTERNS)
            if should_block and resource_type != "document":
                await route.abort()
            else:
                await route.continue_()

        await self.context.route("**/*", route_handler)
        self.adblock_installed = True

    def start_recording(self):
        self._submit(self._start_recording())

    async def _start_recording(self):
        page = self._require_page()
        if not self.record_binding_added:
            await page.expose_binding("__clickRecorderAdd", self._record_click)
            self.record_binding_added = True
        await page.evaluate(RECORDING_SCRIPT)
        self.on_status("Gravando - aperte F8 para parar")

    async def _record_click(self, source, payload):
        self.on_click(dict(payload))

    def stop_recording(self):
        self._submit(self._stop_recording())

    async def _stop_recording(self):
        if self.page and not self.page.is_closed():
            await self.page.evaluate(STOP_RECORDING_SCRIPT)
        self.on_status("Gravacao parada")
        self.on_stopped()

    def execute(self, steps):
        if self.run_task and not self.run_task.done():
            self.on_error("Uma execucao ja esta em andamento.")
            return
        self.run_task = self._submit(self._execute([step.to_dict() for step in steps]))

    async def _execute(self, steps):
        page = self._require_page()
        self.stop_event = asyncio.Event()
        self.on_status("Executando - aperte F8 para parar")
        try:
            while not self.stop_event.is_set():
                for step in steps:
                    if self.stop_event.is_set():
                        break
                    delay = max(0.0, float(step.get("delay", 0.0)))
                    try:
                        await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                        break
                    except asyncio.TimeoutError:
                        pass

                    selector = str(step.get("selector", "")).strip()
                    clicked = False
                    if selector:
                        try:
                            await page.locator(selector).first.click(timeout=1200)
                            clicked = True
                        except Exception:
                            clicked = False
                    if not clicked:
                        await page.mouse.click(float(step.get("x", 0.0)), float(step.get("y", 0.0)))
        finally:
            self.on_status("Execucao parada")
            self.on_stopped()

    def stop_execution(self):
        self._submit(self._stop_execution())

    async def _stop_execution(self):
        if self.stop_event:
            self.stop_event.set()

    def close(self):
        future = asyncio.run_coroutine_threadsafe(self._close(), self.loop)
        try:
            future.result(timeout=10)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)

    async def _close(self):
        if self.stop_event:
            self.stop_event.set()
        await self._close_browser_only()
        if self.playwright:
            await self.playwright.stop()

    async def _close_browser_only(self):
        if self.context:
            await self.context.close()
            self.context = None
        if self.browser:
            await self.browser.close()
            self.browser = None
        self.browser_name = None
        self.browser_incognito = None
        self.page = None
        self.context_incognito = False
        self.record_binding_added = False
        self.adblock_installed = False

    def _require_page(self):
        if self.page is None or self.page.is_closed():
            raise RuntimeError("Clique em 'Abrir navegador' antes.")
        return self.page


RECORDING_SCRIPT = r"""
(() => {
  if (window.__simpleClickRecorderInstalled) return;

  function cssEscape(value) {
    if (window.CSS && window.CSS.escape) return window.CSS.escape(value);
    return String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  function selectorFor(element) {
    if (!element || element.nodeType !== Node.ELEMENT_NODE) return "";
    if (element.id) return "#" + cssEscape(element.id);

    const attrs = ["data-testid", "data-e2e", "aria-label", "name", "title"];
    for (const attr of attrs) {
      const value = element.getAttribute(attr);
      if (value) {
        return element.tagName.toLowerCase() + "[" + attr + "=\"" + value.replace(/"/g, "\\\"") + "\"]";
      }
    }

    const parts = [];
    let current = element;
    while (current && current.nodeType === Node.ELEMENT_NODE && parts.length < 5) {
      let part = current.tagName.toLowerCase();
      const className = String(current.className || "").trim().split(/\s+/).filter(Boolean)[0];
      if (className) part += "." + cssEscape(className);
      const parent = current.parentElement;
      if (parent) {
        const sameTag = Array.from(parent.children).filter(child => child.tagName === current.tagName);
        if (sameTag.length > 1) {
          part += ":nth-of-type(" + (sameTag.indexOf(current) + 1) + ")";
        }
      }
      parts.unshift(part);
      current = parent;
    }
    return parts.join(" > ");
  }

  window.__simpleClickRecorderHandler = event => {
    const target = event.target;
    window.__clickRecorderAdd({
      selector: selectorFor(target),
      x: event.clientX,
      y: event.clientY
    });
  };

  document.addEventListener("click", window.__simpleClickRecorderHandler, true);
  window.__simpleClickRecorderInstalled = true;
})();
"""

STOP_RECORDING_SCRIPT = r"""
(() => {
  if (window.__simpleClickRecorderHandler) {
    document.removeEventListener("click", window.__simpleClickRecorderHandler, true);
  }
  window.__simpleClickRecorderInstalled = false;
})();
"""


class ProfileFrame(ttk.Frame):
    def __init__(self, master, app, profile):
        super().__init__(master, padding=10)
        self.app = app
        self.profile = profile
        self.recording = False
        self.executing = False
        self.last_click_time = None
        self.events = queue.Queue()
        self.browser = BrowserSession(
            self.enqueue_click,
            lambda text: self.enqueue("status", text),
            lambda text: self.enqueue("error", text),
            lambda: self.enqueue("stopped", None),
        )
        self.build_ui()
        self.refresh_steps()
        self.after(100, self.process_events)

    def build_ui(self):
        self.columnconfigure(1, weight=1)
        self.rowconfigure(6, weight=1)

        ttk.Label(self, text="Nome").grid(row=0, column=0, sticky="w")
        self.name_var = tk.StringVar(value=self.profile.name)
        self.name_entry = ttk.Entry(self, textvariable=self.name_var)
        self.name_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0))

        ttk.Label(self, text="Site").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.url_var = tk.StringVar(value=self.profile.url)
        self.url_entry = ttk.Entry(self, textvariable=self.url_var)
        self.url_entry.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))

        ttk.Label(self, text="Navegador").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.browser_var = tk.StringVar(value=normalize_browser_choice(self.profile.browser))
        self.browser_combo = ttk.Combobox(self, textvariable=self.browser_var, values=BROWSER_CHOICES, state="readonly")
        self.browser_combo.grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))

        self.incognito_var = tk.BooleanVar(value=self.profile.incognito)
        ttk.Checkbutton(
            self,
            text="Abrir em guia anonima com adblock",
            variable=self.incognito_var,
            command=self.save_basic_fields,
        ).grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        buttons = ttk.Frame(self)
        buttons.grid(row=4, column=0, columnspan=2, sticky="ew", pady=12)
        for index in range(5):
            buttons.columnconfigure(index, weight=1)

        ttk.Button(buttons, text="1. Abrir navegador", command=self.open_browser).grid(row=0, column=0, sticky="ew", padx=3)
        ttk.Button(buttons, text="2. Gravar", command=self.start_recording).grid(row=0, column=1, sticky="ew", padx=3)
        ttk.Button(buttons, text="3. Executar", command=self.start_execution).grid(row=0, column=2, sticky="ew", padx=3)
        ttk.Button(buttons, text="Parar F8", command=self.stop_action).grid(row=0, column=3, sticky="ew", padx=3)
        ttk.Button(buttons, text="Limpar", command=self.clear_steps).grid(row=0, column=4, sticky="ew", padx=3)

        self.status_var = tk.StringVar(value="Pronto")
        ttk.Label(self, textvariable=self.status_var).grid(row=5, column=0, columnspan=2, sticky="w")

        columns = ("num", "delay", "selector", "pos")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=12)
        self.tree.heading("num", text="#")
        self.tree.heading("delay", text="Tempo")
        self.tree.heading("selector", text="Clique gravado")
        self.tree.heading("pos", text="Posicao")
        self.tree.column("num", width=45, anchor="center")
        self.tree.column("delay", width=90, anchor="center")
        self.tree.column("selector", width=520)
        self.tree.column("pos", width=110, anchor="center")
        self.tree.grid(row=6, column=0, columnspan=2, sticky="nsew")

        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=6, column=2, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.name_var.trace_add("write", lambda *_: self.save_basic_fields())
        self.url_var.trace_add("write", lambda *_: self.save_basic_fields())
        self.browser_var.trace_add("write", lambda *_: self.save_basic_fields())

    def enqueue(self, kind, payload):
        self.events.put((kind, payload))

    def enqueue_click(self, payload):
        self.enqueue("click", payload)

    def process_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "status":
                    self.status_var.set(payload)
                elif kind == "error":
                    self.status_var.set("Erro")
                    messagebox.showwarning("Aviso", payload)
                elif kind == "stopped":
                    self.recording = False
                    self.executing = False
                elif kind == "click":
                    self.add_recorded_click(payload)
        except queue.Empty:
            pass
        self.after(100, self.process_events)

    def save_basic_fields(self):
        self.profile.name = self.name_var.get().strip() or "Perfil"
        self.profile.url = self.url_var.get().strip() or DEFAULT_URL
        self.profile.browser = normalize_browser_choice(self.browser_var.get())
        self.profile.incognito = bool(self.incognito_var.get())
        self.app.rename_current_tab(self.profile.name)
        self.app.save_profiles()

    def open_browser(self):
        self.save_basic_fields()
        self.status_var.set("Abrindo navegador...")
        self.browser.open_browser(self.profile.url, self.profile.browser, self.profile.incognito)

    def start_recording(self):
        self.save_basic_fields()
        self.recording = True
        self.executing = False
        self.last_click_time = None
        self.browser.start_recording()

    def add_recorded_click(self, payload):
        if not self.recording:
            return
        now = time.monotonic()
        delay = 0.0 if self.last_click_time is None else now - self.last_click_time
        self.last_click_time = now
        self.profile.steps.append(
            ClickStep(
                selector=str(payload.get("selector") or ""),
                x=float(payload.get("x") or 0.0),
                y=float(payload.get("y") or 0.0),
                delay=delay,
            )
        )
        self.refresh_steps()
        self.app.save_profiles()

    def start_execution(self):
        if not self.profile.steps:
            messagebox.showinfo("Executar", "Grave pelo menos um clique primeiro.")
            return
        self.save_basic_fields()
        self.recording = False
        self.executing = True
        self.browser.execute(self.profile.steps)

    def stop_action(self):
        if self.recording:
            self.browser.stop_recording()
        if self.executing:
            self.browser.stop_execution()
        self.recording = False
        self.executing = False
        self.status_var.set("Parado")

    def clear_steps(self):
        if not self.profile.steps:
            return
        if messagebox.askyesno("Limpar", "Apagar todos os cliques deste perfil?"):
            self.profile.steps.clear()
            self.refresh_steps()
            self.app.save_profiles()

    def refresh_steps(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for index, step in enumerate(self.profile.steps, start=1):
            selector = step.selector or "(coordenada)"
            self.tree.insert(
                "",
                "end",
                values=(index, f"{step.delay:.2f}s", selector, f"{step.x:.0f}, {step.y:.0f}"),
            )

    def close(self):
        self.browser.close()


class ClickRecorderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Gravador de Cliques")
        self.geometry("950x620")
        self.minsize(760, 480)
        self.store = ProfileStore(PROFILES_FILE)
        self.frames = []
        self.listener = None
        self.build_ui()
        self.load_profiles()
        self.start_hotkey_listener()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def build_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        ttk.Button(top, text="Novo perfil", command=self.add_profile).pack(side="left")
        ttk.Button(top, text="Remover perfil", command=self.remove_current_profile).pack(side="left", padx=(8, 0))
        ttk.Button(top, text="Clonar perfil", command=self.clone_current_profile).pack(side="left", padx=(8, 0))
        ttk.Label(top, text="Use F8 para parar gravacao ou execucao.").pack(side="left", padx=16)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def load_profiles(self):
        for profile in self.store.load():
            self.add_profile(profile)

    def add_profile(self, profile=None):
        if profile is None:
            profile = Profile(name=f"Perfil {len(self.frames) + 1}")
        frame = ProfileFrame(self.notebook, self, profile)
        self.frames.append(frame)
        self.notebook.add(frame, text=profile.name)
        self.notebook.select(frame)
        self.save_profiles()

    def clone_current_profile(self):
        frame = self.current_frame()
        if not frame:
            return

        source_profile = frame.profile
        source_browser = normalize_browser_choice(source_profile.browser)
        suggested_browser = next((choice for choice in BROWSER_CHOICES if choice != source_browser), source_browser)
        browser_choice = simpledialog.askstring(
            "Clonar perfil",
            "Navegador do clone (Brave, Firefox ou Chrome):",
            initialvalue=suggested_browser,
            parent=self,
        )
        if browser_choice is None:
            return

        target_browser = browser_choice.strip() or suggested_browser
        target_browser = normalize_browser_choice(target_browser)
        cloned_profile = Profile(
            name=f"{source_profile.name} - {target_browser}",
            url=source_profile.url,
            browser=target_browser,
            incognito=source_profile.incognito,
            steps=[ClickStep.from_dict(step.to_dict()) for step in source_profile.steps],
        )
        self.add_profile(cloned_profile)

    def remove_current_profile(self):
        if len(self.frames) <= 1:
            messagebox.showinfo("Perfil", "Mantenha pelo menos um perfil.")
            return
        frame = self.current_frame()
        if not frame:
            return
        if not messagebox.askyesno("Remover", "Remover este perfil?"):
            return
        index = self.frames.index(frame)
        frame.close()
        self.notebook.forget(frame)
        self.frames.pop(index)
        frame.destroy()
        self.save_profiles()

    def current_frame(self):
        selected = self.notebook.select()
        if not selected:
            return None
        widget = self.nametowidget(selected)
        return widget if isinstance(widget, ProfileFrame) else None

    def rename_current_tab(self, name):
        frame = self.current_frame()
        if frame:
            self.notebook.tab(frame, text=name or "Perfil")

    def save_profiles(self):
        self.store.save([frame.profile for frame in self.frames])

    def start_hotkey_listener(self):
        if keyboard is None:
            messagebox.showwarning("F8", "pynput nao esta instalado; use o botao Parar.")
            return

        def on_press(key):
            if key == keyboard.Key.f8:
                self.after(0, self.stop_current_action)

        self.listener = keyboard.Listener(on_press=on_press)
        self.listener.daemon = True
        self.listener.start()

    def stop_current_action(self):
        frame = self.current_frame()
        if frame:
            frame.stop_action()

    def on_close(self):
        self.save_profiles()
        if self.listener:
            self.listener.stop()
        for frame in self.frames:
            frame.close()
        self.destroy()


def main():
    app = ClickRecorderApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
