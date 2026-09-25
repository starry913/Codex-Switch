"""Small personal UI: environment + connection + one switch button."""
from __future__ import annotations
import os, queue, threading, tkinter as tk, subprocess, sys
from pathlib import Path
from tkinter import ttk, messagebox
from .bridge import Bridge, distributions
from .core import SwitchError

WSL_PREFIX = "WSL2 · "

class App:
    def __init__(self, root, home=None, store=None):
        self.root=root; self.home=home; self.store=store; self.environment=tk.StringVar(value="Windows"); self.service=tk.StringVar(value="官方直连"); self.status=tk.StringVar(value="正在读取配置…"); self.current=tk.StringVar(value=""); self.events=queue.Queue(); self.busy=False; self.bridge=Bridge(home=home,store=store)
        self.build(); self.root.after(80,self.poll); self.run(lambda:(distributions(),self.bridge.call("diagnostic")),self.loaded)
    def build(self):
        self.root.title("Codex Switch"); self.root.geometry("520x390"); self.root.minsize(480,360); self.root.configure(bg="#F1F4F9")
        icon_root=Path(getattr(sys,"_MEIPASS",Path(__file__).resolve().parent.parent)); icon=icon_root/"assets"/"codex-switch.ico"
        if icon.is_file():
            try:self.root.iconbitmap(default=str(icon))
            except tk.TclError:pass
        style=ttk.Style(); style.theme_use("clam"); style.configure("TFrame",background="#F1F4F9"); style.configure("TLabel",background="#F1F4F9",foreground="#202B43",font=("Microsoft YaHei UI",11)); style.configure("Title.TLabel",font=("Microsoft YaHei UI",23,"bold")); style.configure("TCombobox",padding=8,font=("Microsoft YaHei UI",12)); style.configure("Switch.TButton",background="#3559C7",foreground="white",padding=14,font=("Microsoft YaHei UI",13,"bold")); style.map("Switch.TButton",background=[("active","#2847AB"),("disabled","#9BA9D0")])
        f=ttk.Frame(self.root,padding=34); f.pack(fill="both",expand=True); ttk.Label(f,text="↔  CODEX SWITCH",foreground="#3559C7",font=("Segoe UI",14,"bold")).pack(anchor="w"); ttk.Label(f,text="切一下，继续工作",style="Title.TLabel").pack(anchor="w",pady=(20,5)); ttk.Label(f,text="选择环境和服务，点一次就行。",foreground="#65738B").pack(anchor="w",pady=(0,26))
        ttk.Label(f,text="运行环境").pack(anchor="w",pady=(0,6)); self.env_box=ttk.Combobox(f,textvariable=self.environment,state="readonly"); self.env_box.pack(fill="x",ipady=2); self.env_box.bind("<<ComboboxSelected>>",self.change_environment)
        ttk.Label(f,text="连接服务").pack(anchor="w",pady=(18,6)); self.service_box=ttk.Combobox(f,textvariable=self.service,values=["官方直连","Packy","RightCode"],state="readonly"); self.service_box.pack(fill="x",ipady=2)
        ttk.Button(f,text="立即切换",style="Switch.TButton",command=self.switch).pack(fill="x",pady=(28,16)); ttk.Label(f,textvariable=self.current,foreground="#202B43").pack(anchor="w"); ttk.Label(f,textvariable=self.status,foreground="#65738B",wraplength=450).pack(anchor="w",pady=(8,0))
    def run(self,work,done):
        if self.busy:return
        self.busy=True; self.status.set("处理中…")
        def worker():
            try:self.events.put((True,work(),done))
            except SwitchError as e:self.events.put((False,str(e),None))
            except Exception as e:self.events.put((False,f"操作未完成：{e}",None))
        threading.Thread(target=worker,daemon=True).start()
    def poll(self):
        try:
            ok,result,done=self.events.get_nowait(); self.busy=False
            if ok:done(result)
            else:self.status.set(result); messagebox.showerror("切换失败",result,parent=self.root)
        except queue.Empty:pass
        self.root.after(80,self.poll)
    def loaded(self,result):
        distros,info=result; self.env_box["values"]=["Windows"]+[WSL_PREFIX+x for x in distros]; self.show_info(info)
    def show_info(self,info):
        provider=info.get("provider","openai"); display={"openai":"官方直连","packycode":"Packy","packy":"Packy","rightcode":"RightCode"}.get(provider.lower(),provider); self.current.set(f"当前：{display}    ·    {info.get('home','')}")
        if info.get("processes"):self.status.set("请先退出 Codex、ChatGPT 和 VS Code 的 Codex 后台，再点切换。")
        elif info.get("warnings"):self.status.set(" ".join(info["warnings"]))
        else:self.status.set("准备好了。切换不会动你的历史、记忆和项目文件。")
    def change_environment(self,event=None):
        name=self.environment.get(); wsl=name.startswith(WSL_PREFIX); distro=name.removeprefix(WSL_PREFIX).strip() if wsl else None; self.bridge=Bridge(distro=distro or None,home=None if wsl else self.home,store=None if wsl else self.store); self.run(lambda:self.bridge.call("diagnostic"),self.show_info)
    def switch(self):
        if self.busy:return
        wanted=self.service.get(); windows=not self.environment.get().startswith(WSL_PREFIX)
        # Restart inside the selected environment. Only the Windows desktop app is launched from here.
        self.run(lambda:self.bridge.call("quick_switch",service=wanted,restart=True),lambda result:self.done(result,windows))
    def launch_desktop(self,app_id):
        if os.name!="nt":return
        # Reopen the packaged desktop app. Launching ChatGPT.exe under WindowsApps is denied.
        app_id=app_id or "OpenAI.Codex_2p2nqsd0c76g0!App"
        try:
            startupinfo=subprocess.STARTUPINFO(); startupinfo.dwFlags|=subprocess.STARTF_USESHOWWINDOW; startupinfo.wShowWindow=subprocess.SW_HIDE
            subprocess.Popen(["explorer.exe","shell:AppsFolder\\"+app_id],startupinfo=startupinfo,creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError:pass
    def done(self,result,launch=False):
        if launch:self.launch_desktop(result.get("app_id"))
        message=result.get("message") or "已切换。"
        self.status.set(message); messagebox.showinfo("切换完成",message,parent=self.root); self.run(lambda:self.bridge.call("diagnostic"),self.show_info)

def launch(home=None,store=None):
    root=tk.Tk(); App(root,home,store); root.mainloop()
