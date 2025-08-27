from __future__ import annotations
import tkinter as tk
from tkinter import scrolledtext
from tkinter import ttk
from googlesearch import search
import requests
from bs4 import BeautifulSoup
from bs4.element import Tag
import json
import re
import threading
import webbrowser
from tkinter import filedialog, messagebox
from openpyxl import Workbook
from urllib.parse import urlparse
from typing import Optional, List, Dict, Any

# -------------------------------------------
# Listas e cache
# -------------------------------------------

UFS = [
    ("AC", "Acre"), ("AL", "Alagoas"), ("AP", "Amapá"), ("AM", "Amazonas"),
    ("BA", "Bahia"), ("CE", "Ceará"), ("DF", "Distrito Federal"), ("ES", "Espírito Santo"),
    ("GO", "Goiás"), ("MA", "Maranhão"), ("MT", "Mato Grosso"), ("MS", "Mato Grosso do Sul"),
    ("MG", "Minas Gerais"), ("PA", "Pará"), ("PB", "Paraíba"), ("PR", "Paraná"),
    ("PE", "Pernambuco"), ("PI", "Piauí"), ("RJ", "Rio de Janeiro"), ("RN", "Rio Grande do Norte"),
    ("RS", "Rio Grande do Sul"), ("RO", "Rondônia"), ("RR", "Roraima"), ("SC", "Santa Catarina"),
    ("SP", "São Paulo"), ("SE", "Sergipe"), ("TO", "Tocantins")
]
MUNICIPIOS_CACHE: Dict[str, List[str]] = {}
SEARCH_RESULTS: List[Dict[str, Any]] = []

# Flags/globais para barra de progresso
CANCELAR = False
TOTAL_PASSOS = 0
PASSOS_CONCLUIDOS = 0

# Widgets globais (opcionais para satisfazer analisadores estáticos/Pylance)
root: Optional[tk.Tk] = None
estado_combo: Optional[ttk.Combobox] = None
cidade_combo: Optional[ttk.Combobox] = None
resultado_text: Optional[tk.Text] = None
btn_buscar: Optional[tk.Button] = None
btn_limpar: Optional[tk.Button] = None
btn_planilha: Optional[tk.Button] = None
btn_cancelar: Optional[tk.Button] = None
status_label: Optional[ttk.Label] = None
progress: Optional[ttk.Progressbar] = None

# -------------------------------------------
# Utilitários
# -------------------------------------------

def _headers():
    return {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}


def _limpa_tel(t: str) -> str:
    return re.sub(r'\D', '', t or '')


def _uniq(seq):
    return list(dict.fromkeys([s for s in seq if s]))


def _iter_hrefs(soup: BeautifulSoup):
    """
    Itera com segurança por todos os hrefs:
    lida com href sendo str OU lista (AttributeValueList).
    Evita erros de tipagem do Pylance/BS4.
    """
    for node in soup.find_all('a'):
        if not isinstance(node, Tag):
            continue
        href_val = node.get('href', None)
        if isinstance(href_val, list):
            for hv in href_val:
                if isinstance(hv, str):
                    yield hv
        elif isinstance(href_val, str):
            yield href_val


def _extrai_jsonld(soup: BeautifulSoup):
    """Extrai telefone/e-mail/endereço/redes de possíveis blocos JSON-LD."""
    tels, emails, enderecos, redes = [], [], [], []
    for node in soup.find_all('script', type='application/ld+json'):
        if not isinstance(node, Tag):
            continue
        try:
            raw = node.string if hasattr(node, "string") and node.string is not None else node.get_text()  # type: ignore[attr-defined]
            data = json.loads(raw or '')
        except Exception:
            continue
        blocos = data if isinstance(data, list) else [data]
        for b in blocos:
            if not isinstance(b, dict):
                continue
            tel = b.get('telephone')
            if isinstance(tel, list):
                tels.extend(tel)
            elif tel:
                tels.append(tel)

            email = b.get('email')
            if isinstance(email, list):
                emails.extend(email)
            elif email:
                emails.append(email)

            addr = b.get('address')
            if isinstance(addr, dict):
                linha = " ".join(_uniq([
                    addr.get('streetAddress', ''),
                    addr.get('addressLocality', ''),
                    addr.get('addressRegion', ''),
                    addr.get('postalCode', ''),
                    addr.get('addressCountry', ''),
                ])).strip()
                if linha:
                    enderecos.append(linha)

            same_as = b.get('sameAs')
            if isinstance(same_as, list):
                redes.extend(same_as)
            elif isinstance(same_as, str):
                redes.append(same_as)

    return _uniq(tels), _uniq(emails), _uniq(enderecos), _uniq(redes)

# -------------------------------------------
# Busca (Google)
# -------------------------------------------

def buscar_sites(consulta, num_sites=10):
    """
    Compatível com 'googlesearch' e 'googlesearch-python' sem depender de kwargs específicos.
    Busca e limita localmente a quantidade retornada.
    """
    try:
        results = list(search(consulta))
    except TypeError:
        try:
            results = list(search(consulta))
        except Exception as e2:
            print(f"Erro ao buscar sites para '{consulta}': {e2}")
            return []
    except Exception as e:
        print(f"Erro ao buscar sites para '{consulta}': {e}")
        return []
    return results[:num_sites]

# -------------------------------------------
# UI helpers (links clicáveis, render, etc.)
# -------------------------------------------

def insert_link(widget: tk.Text, url: str, display_text: Optional[str] = None):
    """Insere um link clicável com cursor de mão."""
    if not display_text:
        display_text = url
    start = widget.index(tk.END)
    widget.insert(tk.END, display_text)
    end = widget.index(tk.END)
    tag = f"link_{start.replace('.', '_')}"
    widget.tag_add(tag, start, end)
    widget.tag_config(tag, foreground="blue", underline=True)

    def _open(_e=None, link=url):
        try:
            webbrowser.open_new(link)
        except Exception:
            pass

    def _enter(_e=None):
        widget.config(cursor="hand2")

    def _leave(_e=None):
        widget.config(cursor="arrow")

    widget.tag_bind(tag, "<Button-1>", _open)
    widget.tag_bind(tag, "<Enter>", _enter)
    widget.tag_bind(tag, "<Leave>", _leave)


def render_results(results: List[Dict[str, Any]]):
    """Mostra resultados com links clicáveis (urls, e-mails, telefones)."""
    if resultado_text is None:
        return
    try:
        resultado_text.delete(1.0, tk.END)
        resultado_text.insert(tk.END, "=== Resultados ===\n")

        if not results:
            resultado_text.insert(tk.END, "\nNenhum site foi encontrado para a consulta.\n")
            return

        for item in results:
            site = item.get("site", "")
            emails = item.get("emails", [])
            telefones = item.get("telefones", [])
            enderecos = item.get("enderecos", [])
            outros_sites = item.get("outros_sites", [])
            redes_sociais = item.get("redes_sociais", [])

            resultado_text.insert(tk.END, "\n🔗 Site: ")
            if site:
                insert_link(resultado_text, site, site)
            resultado_text.insert(tk.END, "\n")

            if emails:
                resultado_text.insert(tk.END, "📧 E-mails encontrados:\n")
                for email in emails:
                    resultado_text.insert(tk.END, "   - ")
                    insert_link(resultado_text, f"mailto:{email}", email)
                    resultado_text.insert(tk.END, "\n")
            else:
                resultado_text.insert(tk.END, "Nenhum e-mail encontrado.\n")

            if telefones:
                resultado_text.insert(tk.END, "📞 Telefones encontrados:\n")
                for tel in telefones:
                    resultado_text.insert(tk.END, "   - ")
                    if isinstance(tel, str) and tel.lower() == "whatsapp":
                        resultado_text.insert(tk.END, "WhatsApp\n")
                    else:
                        tel_digits = re.sub(r"\D", "", tel)
                        if tel_digits:
                            insert_link(resultado_text, f"tel:{tel_digits}", tel)
                            resultado_text.insert(tk.END, "\n")
                        else:
                            resultado_text.insert(tk.END, f"{tel}\n")
            else:
                resultado_text.insert(tk.END, "Nenhum telefone encontrado.\n")

            if enderecos:
                resultado_text.insert(tk.END, "🏠 Endereços encontrados:\n")
                for end in enderecos:
                    resultado_text.insert(tk.END, f"   - {end}\n")
            else:
                resultado_text.insert(tk.END, "Nenhum endereço encontrado.\n")

            if outros_sites:
                resultado_text.insert(tk.END, "🌐 Outros sites encontrados:\n")
                for s in outros_sites:
                    resultado_text.insert(tk.END, "   - ")
                    insert_link(resultado_text, s, s)
                    resultado_text.insert(tk.END, "\n")
            else:
                resultado_text.insert(tk.END, "Nenhum site encontrado.\n")

            if redes_sociais:
                resultado_text.insert(tk.END, "🔗 Redes sociais encontradas:\n")
                for r in redes_sociais:
                    resultado_text.insert(tk.END, "   - ")
                    insert_link(resultado_text, r, r)
                    resultado_text.insert(tk.END, "\n")
            else:
                resultado_text.insert(tk.END, "Nenhuma rede social encontrada.\n")

    except Exception as e:
        print(f"Erro ao renderizar resultados: {e}")

# -------------------------------------------
# UF / Município
# -------------------------------------------

def carregar_estados():
    if estado_combo is None or cidade_combo is None:
        return
    ufs_legiveis = [f"{sigla} - {nome}" for sigla, nome in UFS]
    estado_combo["values"] = ufs_legiveis
    estado_combo.set("")
    cidade_combo.set("")
    cidade_combo["values"] = []


def on_estado_selecionado(event=None):
    if estado_combo is None or cidade_combo is None:
        return
    valor = estado_combo.get().strip()
    if not valor:
        cidade_combo["values"] = []
        cidade_combo.set("")
        return
    sigla = valor.split(" - ")[0]
    if sigla in MUNICIPIOS_CACHE:
        cidade_combo["values"] = MUNICIPIOS_CACHE[sigla]
        cidade_combo.set("")
        return
    try:
        url = f"https://servicodados.ibge.gov.br/api/v1/localidades/estados/{sigla}/municipios"
        resp = requests.get(url, headers=_headers(), timeout=10)
        if resp.status_code == 200:
            dados = resp.json()
            cidades = sorted([item.get("nome", "") for item in dados if item.get("nome")])
            MUNICIPIOS_CACHE[sigla] = cidades
            cidade_combo["values"] = cidades
            cidade_combo.set("")
        else:
            cidade_combo["values"] = []
            cidade_combo.set("")
        if root is not None:
            root.update_idletasks()
    except Exception as e:
        print(f"Erro ao buscar municípios do IBGE ({sigla}): {e}")
        cidade_combo["values"] = []
        cidade_combo.set("")


def get_localidade_text():
    if estado_combo is None or cidade_combo is None:
        return ""
    uf = estado_combo.get().strip()
    cidade = cidade_combo.get().strip()
    sigla = uf.split(" - ")[0] if (" - " in uf) else uf
    if cidade and sigla:
        return f"{cidade} - {sigla}"
    if sigla:
        return sigla
    return ""

# -------------------------------------------
# Progresso (UI)
# -------------------------------------------

def _ui_begin_indeterminado():
    if status_label is not None:
        status_label.config(text="Procurando…")
    if progress is not None:
        progress.configure(mode="indeterminate")
        progress.start(12)
    if btn_buscar is not None:
        btn_buscar.config(state="disabled")
    if btn_limpar is not None:
        btn_limpar.config(state="disabled")
    if btn_planilha is not None:
        btn_planilha.config(state="disabled")
    if btn_cancelar is not None:
        btn_cancelar.config(state="normal")


def _ui_begin_determinado(total: int):
    if status_label is not None:
        status_label.config(text="Procurando (0/{} – 0%)…".format(total))
    if progress is not None:
        progress.configure(mode="determinate", maximum=total)
        progress['value'] = 0
    if btn_buscar is not None:
        btn_buscar.config(state="disabled")
    if btn_limpar is not None:
        btn_limpar.config(state="disabled")
    if btn_planilha is not None:
        btn_planilha.config(state="disabled")
    if btn_cancelar is not None:
        btn_cancelar.config(state="normal")


def _ui_step(atual: int, total: int, dominio: str = ""):
    if progress is not None:
        progress['value'] = atual
    pct = int((atual / total) * 100) if total else 0
    label_dom = f" – {dominio}" if dominio else ""
    if status_label is not None:
        status_label.config(text=f"Procurando ({atual}/{total} – {pct}%){label_dom}")


def _ui_end(cancelado: bool = False):
    try:
        if progress is not None:
            progress.stop()
    except Exception:
        pass
    if btn_buscar is not None:
        btn_buscar.config(state="normal")
    if btn_limpar is not None:
        btn_limpar.config(state="normal")
    if btn_planilha is not None:
        btn_planilha.config(state="normal")
    if btn_cancelar is not None:
        btn_cancelar.config(state="disabled")
    if status_label is not None:
        status_label.config(text="Cancelado" if cancelado else "Concluído!")


def cancelar_busca():
    global CANCELAR
    CANCELAR = True

# -------------------------------------------
# Thread de busca (com passos granulares)
# -------------------------------------------

def _calc_passos_por_site(flags: Dict[str, bool]) -> int:
    # Base: 2 passos (requisição + parse)
    passos = 2
    # Cada tipo de extração vira um passo
    if flags['email']:
        passos += 1
    if flags['tel']:
        passos += 1
    if flags['endereco']:
        passos += 1
    # Varredura de hrefs (sites/redes) é mais um passo se alguma dessas estiver ativa
    if flags['site'] or flags['social']:
        passos += 1
    return passos


def buscar_thread():
    global CANCELAR, TOTAL_PASSOS, PASSOS_CONCLUIDOS, SEARCH_RESULTS
    CANCELAR = False
    PASSOS_CONCLUIDOS = 0

    busca = entry_busca.get().strip() if 'entry_busca' in globals() else ''
    localidade = entry_localidade.get().strip() if 'entry_localidade' in globals() else ''
    cidade = get_localidade_text()
    flags = {
        'email': var_email.get() if 'var_email' in globals() else True,
        'tel': var_tel.get() if 'var_tel' in globals() else True,
        'endereco': var_endereco.get() if 'var_endereco' in globals() else False,
        'site': var_site.get() if 'var_site' in globals() else False,
        'social': var_social.get() if 'var_social' in globals() else False,
    }

    if not any(flags.values()):
        if resultado_text is not None:
            resultado_text.delete(1.0, tk.END)
            resultado_text.insert(tk.END, "Selecione pelo menos uma opção para buscar.\n")
        _ui_end()
        return
    if not busca:
        if resultado_text is not None:
            resultado_text.delete(1.0, tk.END)
            resultado_text.insert(tk.END, "Digite o que você quer procurar no primeiro campo.\n")
        _ui_end()
        return

    consulta = " ".join([p for p in [busca, localidade, cidade] if p])

    # Fase 1: descobrir sites (indeterminado)
    if root is not None:
        root.after(0, _ui_begin_indeterminado)
    sites = buscar_sites(consulta, num_sites=10)

    if CANCELAR:
        if root is not None:
            root.after(0, _ui_end, True)
        return
    if not sites:
        def _no_sites():
            if resultado_text is not None:
                resultado_text.delete(1.0, tk.END)
                resultado_text.insert(tk.END, "Nenhum site foi encontrado para a consulta.\n")
        if root is not None:
            root.after(0, _no_sites)
            root.after(0, _ui_end)
        return

    # Fase 2: processar sites (determinado com passos granulares)
    passos_por_site = _calc_passos_por_site(flags)
    TOTAL_PASSOS = len(sites) * passos_por_site
    PASSOS_CONCLUIDOS = 0
    if root is not None:
        root.after(0, _ui_begin_determinado, TOTAL_PASSOS)

    results: List[Dict[str, Any]] = []

    for site in sites:
        if CANCELAR:
            break

        dominio = urlparse(site).netloc or site

        # --- Passo 1: requisição ---
        try:
            resp = requests.get(site, headers=_headers(), timeout=10)
            status_ok = (resp.status_code == 200)
        except Exception:
            status_ok = False
            resp = None
        PASSOS_CONCLUIDOS += 1
        if root is not None:
            root.after(0, _ui_step, PASSOS_CONCLUIDOS, TOTAL_PASSOS, dominio)
        if CANCELAR:
            break
        if not status_ok:
            # Registra item vazio e segue
            results.append({
                "site": site,
                "emails": [],
                "telefones": [],
                "enderecos": [],
                "outros_sites": [],
                "redes_sociais": [],
            })
            continue

        # --- Passo 2: parse ---
        soup = BeautifulSoup(resp.text, 'html.parser')
        texto = soup.get_text(separator=' ', strip=True)
        PASSOS_CONCLUIDOS += 1
        if root is not None:
            root.after(0, _ui_step, PASSOS_CONCLUIDOS, TOTAL_PASSOS, dominio)
        if CANCELAR:
            break

        # Extra pre-load de json-ld (não conta passo, só otimiza)
        tel_ld, email_ld, end_ld, redes_ld = _extrai_jsonld(soup)

        emails, telefones, enderecos, outros_sites, redes_sociais = [], [], [], [], []

        # --- Passo 3: e-mails ---
        if flags['email']:
            try:
                emails = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", texto)
                for href in _iter_hrefs(soup):
                    if isinstance(href, str) and href.lower().startswith('mailto:'):
                        emails.append(href.replace('mailto:', '').split('?')[0])
                emails = _uniq(emails + email_ld)
            except Exception:
                pass
            PASSOS_CONCLUIDOS += 1
            if root is not None:
                root.after(0, _ui_step, PASSOS_CONCLUIDOS, TOTAL_PASSOS, dominio)
            if CANCELAR:
                break

        # --- Passo 4: telefones ---
        if flags['tel']:
            try:
                for h in _iter_hrefs(soup):
                    if not isinstance(h, str):
                        continue
                    low = h.lower()
                    if low.startswith('tel:'):
                        telefones.append(h.replace('tel:', ''))
                    if 'wa.me/' in low or 'whatsapp.com/send' in low:
                        telefones.append('WhatsApp')
                telefones += re.findall(r"\(?\d{2}\)?\s?\d{4,5}[-\s]?\d{4}", texto)
                telefones = _uniq([t for t in telefones if len(_limpa_tel(t)) >= 10 or t == 'WhatsApp'])
                telefones = _uniq(telefones + tel_ld)
            except Exception:
                pass
            PASSOS_CONCLUIDOS += 1
            if root is not None:
                root.after(0, _ui_step, PASSOS_CONCLUIDOS, TOTAL_PASSOS, dominio)
            if CANCELAR:
                break

        # --- Passo 5: endereços ---
        if flags['endereco']:
            try:
                padrao_end = r"\b(?:Rua|Avenida|Av\.|Travessa|Praça|Rodovia|Estrada|Alameda|Largo|BR-|SP-|RJ-)\s+[^\n,]{3,120}"
                enderecos = re.findall(padrao_end, texto)
                enderecos = _uniq(enderecos + end_ld)
            except Exception:
                pass
            PASSOS_CONCLUIDOS += 1
            if root is not None:
                root.after(0, _ui_step, PASSOS_CONCLUIDOS, TOTAL_PASSOS, dominio)
            if CANCELAR:
                break

        # --- Passo 6: outros sites / redes sociais ---
        if flags['site'] or flags['social']:
            try:
                for href in _iter_hrefs(soup):
                    if not isinstance(href, str):
                        continue
                    low = href.lower()
                    if not low.startswith('http'):
                        continue
                    if flags['social'] and any(s in low for s in ['facebook.com', 'instagram.com', 'twitter.com', 'x.com', 'linkedin.com', 'wa.me', 'whatsapp.com']):
                        redes_sociais.append(href)
                    elif flags['site']:
                        outros_sites.append(href)
                outros_sites = _uniq(outros_sites)
                redes_sociais = _uniq(redes_sociais + redes_ld)
            except Exception:
                pass
            PASSOS_CONCLUIDOS += 1
            if root is not None:
                root.after(0, _ui_step, PASSOS_CONCLUIDOS, TOTAL_PASSOS, dominio)
            if CANCELAR:
                break

        results.append({
            "site": site,
            "emails": emails if flags['email'] else [],
            "telefones": telefones if flags['tel'] else [],
            "enderecos": enderecos if flags['endereco'] else [],
            "outros_sites": outros_sites if flags['site'] else [],
            "redes_sociais": redes_sociais if flags['social'] else [],
        })

    def _apply_results():
        global SEARCH_RESULTS
        SEARCH_RESULTS = results
        render_results(results)
        _ui_end(CANCELAR)

    if root is not None:
        root.after(0, _apply_results)

# -------------------------------------------
# Ações de UI
# -------------------------------------------

def buscar():
    threading.Thread(target=buscar_thread, daemon=True).start()


def limpar_total():
    try:
        if 'entry_busca' in globals():
            entry_busca.delete(0, tk.END)
        if 'entry_localidade' in globals():
            entry_localidade.delete(0, tk.END)
        if estado_combo is not None:
            estado_combo.set("")
        if cidade_combo is not None:
            cidade_combo.set("")
            cidade_combo["values"] = []
        if 'var_email' in globals():
            var_email.set(True)
        if 'var_tel' in globals():
            var_tel.set(True)
        if 'var_endereco' in globals():
            var_endereco.set(False)
        if 'var_site' in globals():
            var_site.set(False)
        if 'var_social' in globals():
            var_social.set(False)
        if resultado_text is not None:
            resultado_text.delete(1.0, tk.END)
        global SEARCH_RESULTS
        SEARCH_RESULTS = []
        if status_label is not None:
            status_label.config(text="Pronto")
        try:
            if progress is not None:
                progress.stop()
        except Exception:
            pass
        if progress is not None:
            progress['value'] = 0
    except Exception as e:
        print(f"Erro ao limpar: {e}")


def gerar_planilha():
    try:
        if not SEARCH_RESULTS:
            messagebox.showinfo("Gerar planilha", "Nenhum dado para exportar. Faça uma busca primeiro.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel", "*.xlsx")],
            title="Salvar planilha"
        )
        if not path:
            return

        wb = Workbook()
        default_ws = wb.active
        if default_ws is not None:
            wb.remove(default_ws)
        ws = wb.create_sheet(title="Resultados", index=0)

        ws.append(["Site", "E-mails", "Telefones", "Endereços", "Outros Sites", "Redes Sociais"])
        for item in SEARCH_RESULTS:
            ws.append([
                item.get("site", ""),
                ", ".join(item.get("emails", [])),
                ", ".join(item.get("telefones", [])),
                " | ".join(item.get("enderecos", [])),
                ", ".join(item.get("outros_sites", [])),
                ", ".join(item.get("redes_sociais", [])),
            ])
        wb.save(path)
        messagebox.showinfo("Gerar planilha", f"Planilha salva em:\n{path}")
    except Exception as e:
        messagebox.showerror("Gerar planilha", f"Erro ao salvar planilha:\n{e}")

# -------------------------------------------
# Interface gráfica
# -------------------------------------------

root = tk.Tk()
root.title("Raspador de Email")
root.geometry("1100x820")
root.minsize(900, 640)

# Inputs
frm_inputs = tk.Frame(root)
frm_inputs.pack(fill="x", padx=10, pady=(10, 0))

lbl1 = tk.Label(frm_inputs, text="O que você quer procurar?")
lbl1.grid(row=0, column=0, sticky="w")
entry_busca = tk.Entry(frm_inputs, width=60)
entry_busca.grid(row=1, column=0, sticky="w")

lbl2 = tk.Label(frm_inputs, text="Bairro/Localidade (opcional)")
lbl2.grid(row=0, column=1, sticky="w", padx=(20, 0))
entry_localidade = tk.Entry(frm_inputs, width=40)
entry_localidade.grid(row=1, column=1, sticky="w", padx=(20, 0))

lbl3 = tk.Label(frm_inputs, text="Estado (UF)")
lbl3.grid(row=2, column=0, sticky="w", pady=(10, 0))
estado_combo = ttk.Combobox(frm_inputs, width=30, state="readonly")
estado_combo.grid(row=3, column=0, sticky="w")
estado_combo.bind("<<ComboboxSelected>>", on_estado_selecionado)

lbl4 = tk.Label(frm_inputs, text="Cidade (município)")
lbl4.grid(row=2, column=1, sticky="w", pady=(10, 0), padx=(20, 0))
cidade_combo = ttk.Combobox(frm_inputs, width=40)
cidade_combo.grid(row=3, column=1, sticky="w", padx=(20, 0))

# Opções de busca
frame_opcoes = tk.Frame(root)
frame_opcoes.pack(fill="x", pady=5)

var_email = tk.BooleanVar(value=True)
var_tel = tk.BooleanVar(value=True)
var_endereco = tk.BooleanVar(value=False)
var_site = tk.BooleanVar(value=False)
var_social = tk.BooleanVar(value=False)

tk.Checkbutton(frame_opcoes, text="E-mail", variable=var_email).pack(side=tk.LEFT, padx=5)
tk.Checkbutton(frame_opcoes, text="Telefone", variable=var_tel).pack(side=tk.LEFT, padx=5)
tk.Checkbutton(frame_opcoes, text="Endereço", variable=var_endereco).pack(side=tk.LEFT, padx=5)
tk.Checkbutton(frame_opcoes, text="Site", variable=var_site).pack(side=tk.LEFT, padx=5)
tk.Checkbutton(frame_opcoes, text="Rede Social", variable=var_social).pack(side=tk.LEFT, padx=5)

# Botões e barra de progresso
btn_frame = tk.Frame(root)
btn_frame.pack(fill="x", pady=10)

btn_buscar = tk.Button(btn_frame, text="Buscar", command=buscar)
btn_buscar.pack(side=tk.LEFT, padx=5)

btn_limpar = tk.Button(btn_frame, text="Limpar", command=limpar_total)
btn_limpar.pack(side=tk.LEFT, padx=5)

btn_planilha = tk.Button(btn_frame, text="Gerar Planilha", command=gerar_planilha)
btn_planilha.pack(side=tk.LEFT, padx=5)

btn_cancelar = tk.Button(btn_frame, text="Cancelar", command=cancelar_busca, state="disabled")
btn_cancelar.pack(side=tk.LEFT, padx=5)

status_label = ttk.Label(btn_frame, text="Pronto")
status_label.pack(side=tk.RIGHT, padx=10)

progress = ttk.Progressbar(root, mode="determinate")
progress.pack(fill="x", padx=10)

# Área de resultados
resultado_frame = tk.Frame(root)
resultado_frame.pack(fill="both", expand=True, padx=10, pady=10)
resultado_text = scrolledtext.ScrolledText(resultado_frame, wrap="word")
resultado_text.pack(fill="both", expand=True)

# Somente leitura sem state='disabled' (para manter links clicáveis)

def _make_text_readonly(widget: tk.Text):
    for seq in ("<Key>", "<Control-v>", "<Control-V>", "<<Paste>>",
                "<Button-2>", "<BackSpace>", "<Delete>",
                "<Control-x>", "<Control-X>"):
        widget.bind(seq, lambda e: "break")
    widget.config(cursor="arrow")

_make_text_readonly(resultado_text)

carregar_estados()
root.mainloop()