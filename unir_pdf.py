"""Unir PDF: junta PDFs e imágenes escaneadas en un solo PDF, todo en local.

No usa internet. Solo lee los archivos que eliges y escribe el PDF que guardas.
Uso: doble clic en UnirPDF.bat, o arrastra archivos encima de UnirPDF.bat.
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable

import pypdfium2 as pdfium
from PIL import Image, ImageOps, ImageSequence, ImageTk
from pypdf import PdfReader, PdfWriter

EXT_PDF = {".pdf"}
EXT_IMG = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
LADO_HOJA_PULG = 11      # sin ppp creíbles, el lado mayor de la hoja mide esto (carta)
LADO_MAX_PULG = 17       # más grande que tabloide: el tamaño no es creíble (p. ej. foto a 72 ppp)
MINI = (64, 84)          # miniatura en la lista de hojas (a escala 100 %)


def escalador(w: tk.Misc) -> Callable[[float], int]:
    """Convierte medidas pensadas a 100 % a la escala de pantalla de Windows."""
    factor = w.winfo_fpixels("1i") / 96
    return lambda n: int(round(n * factor))


def alto_renglon(w: tk.Misc, px: Callable[[float], int]) -> int:
    return tkfont.nametofont("TkDefaultFont", root=w).metrics("linespace") + px(8)


def con_borde(img: Image.Image) -> Image.Image:
    """Borde gris para que una hoja blanca se distinga del fondo blanco."""
    return ImageOps.expand(img.convert("RGB"), border=1, fill="#999999")


@dataclass
class Elemento:
    ruta: Path
    lector: PdfReader
    datos: bytes                      # el PDF en bytes, para dibujar las hojas
    clave: str | None = None
    giro: int = 0                     # giro de todo el archivo (0, 90, 180, 270)
    orden: list[int] = field(default_factory=list)       # hojas originales, en el orden final
    giros_hoja: dict[int, int] = field(default_factory=dict)  # giro extra por hoja original

    def __post_init__(self):
        if not self.orden:
            self.orden = list(range(self.total))

    @property
    def total(self) -> int:
        return len(self.lector.pages)

    @property
    def hojas(self) -> int:
        return len(self.orden)

    @property
    def editado(self) -> bool:
        return self.orden != list(range(self.total)) or any(self.giros_hoja.values())

    def giro_de(self, hoja: int) -> int:
        return (self.giro + self.giros_hoja.get(hoja, 0)) % 360


def orden_natural(ruta: Path):
    """'scan2' antes que 'scan10'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", ruta.name)]


def mover_indices(lista: list, sel: list[int], paso: int) -> list[int]:
    """Mueve los elementos seleccionados un lugar arriba (-1) o abajo (+1)."""
    if not sel or (paso < 0 and sel[0] == 0) or (paso > 0 and sel[-1] == len(lista) - 1):
        return sel
    for i in (sel if paso < 0 else reversed(sel)):
        lista[i], lista[i + paso] = lista[i + paso], lista[i]
    return [i + paso for i in sel]


def imagen_a_pdf(ruta: Path) -> bytes:
    """Convierte una imagen (o un TIFF de varias hojas) en un PDF en memoria."""
    with Image.open(ruta) as img:
        dpi = img.info.get("dpi")
        hojas = []
        for cuadro in ImageSequence.Iterator(img):
            hoja = ImageOps.exif_transpose(cuadro)  # respeta la orientación de la cámara
            if hoja.mode not in ("1", "L", "RGB"):
                hoja = hoja.convert("RGB")
            hojas.append(hoja.copy())
    lado_px = max(hojas[0].size)
    if dpi and dpi[0] >= 50 and lado_px / dpi[0] <= LADO_MAX_PULG:
        resolucion = float(dpi[0])
    else:
        # Sin ppp, o con ppp que darían una hoja enorme (los celulares marcan 72 ppp
        # en fotos de miles de pixeles): se ajusta a tamaño carta.
        resolucion = lado_px / LADO_HOJA_PULG
    salida = io.BytesIO()
    hojas[0].save(salida, "PDF", resolution=resolucion, save_all=True,
                  append_images=hojas[1:])
    return salida.getvalue()


def cargar(ruta: Path, pedir_clave: Callable[[str], str | None]) -> Elemento:
    ext = ruta.suffix.lower()
    clave = None
    if ext in EXT_PDF:
        datos = ruta.read_bytes()
        lector = PdfReader(io.BytesIO(datos))
        if lector.is_encrypted and not lector.decrypt(""):
            clave = pedir_clave(ruta.name)
            if not clave or not lector.decrypt(clave):
                raise ValueError("está protegido con contraseña y no se pudo abrir")
    elif ext in EXT_IMG:
        datos = imagen_a_pdf(ruta)
        lector = PdfReader(io.BytesIO(datos))
    else:
        raise ValueError("no es PDF ni imagen")
    if len(lector.pages) == 0:
        raise ValueError("no tiene hojas")
    return Elemento(ruta, lector, datos, clave)


# Niveles de compresión, de mejor a peor calidad: (puntos por pulgada, calidad JPEG).
# 150 ppp se lee bien en pantalla e impreso; 100 ppp es el mínimo razonable para texto.
NIVELES = [(200, 80), (150, 70), (150, 55), (120, 50), (100, 40)]


@dataclass
class Resultado:
    hojas: int
    peso: int                            # bytes del PDF guardado
    peso_original: int                   # bytes que pesaría sin comprimir
    nivel: tuple[int, int] | None = None  # (ppp, calidad) si se comprimió
    alcanzo_meta: bool = True


def tamano(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB" if n >= 1_000_000 else f"{max(n // 1000, 1)} KB"


def tiene_imagenes(pagina) -> bool:
    try:
        return len(pagina.images) > 0
    except Exception:
        return True  # si no se puede saber, se trata como escaneo


def construir(elementos: list[Elemento], nivel: tuple[int, int] | None = None,
              grises: bool = False, docs: dict | None = None, cache: dict | None = None,
              avance: Callable[[str], None] | None = None) -> tuple[bytes, int]:
    """Arma el PDF en memoria y regresa (bytes, hojas).

    Con nivel=(ppp, calidad), las hojas que tienen imágenes (escaneos) se vuelven a
    dibujar como JPEG a esa resolución; las hojas de solo texto se copian tal cual.
    """
    docs = {} if docs is None else docs
    cache = {} if cache is None else cache
    escritor = PdfWriter()
    total = sum(el.hojas for el in elementos)
    hechas = 0
    for el in elementos:
        for hoja in el.orden:
            pagina = el.lector.pages[hoja]
            giro = el.giro_de(hoja)
            if nivel and tiene_imagenes(pagina):
                ppp, calidad = nivel
                llave = (id(el), hoja, ppp, giro, grises)
                if llave not in cache:
                    if id(el) not in docs:
                        docs[id(el)] = pdfium.PdfDocument(el.datos, password=el.clave or None)
                    pagina_pdfium = docs[id(el)][hoja]
                    escala = ppp / 72
                    lado_pt = max(pagina_pdfium.get_size())
                    if lado_pt / 72 > LADO_MAX_PULG:
                        # Hoja de tamaño irreal: se dibuja como si fuera carta, pero se
                        # conserva su tamaño de página (solo baja la densidad de pixeles).
                        escala = LADO_HOJA_PULG * ppp / lado_pt
                    img = pagina_pdfium.render(scale=escala).to_pil()
                    if giro:
                        img = img.rotate(-giro, expand=True)
                    cache[llave] = (img.convert("L" if grises else "RGB"), escala * 72)
                img, resolucion = cache[llave]
                buf = io.BytesIO()
                img.save(buf, "PDF", resolution=resolucion, quality=calidad, optimize=True)
                escritor.add_page(PdfReader(buf).pages[0])
            else:
                copia = escritor.add_page(pagina)  # copia: el original no se toca
                if giro:
                    copia.rotate(giro)
            hechas += 1
            if avance:
                etapa = f"Comprimiendo a {nivel[0]} ppp, calidad {nivel[1]}" if nivel else "Uniendo"
                avance(f"{etapa}: hoja {hechas} de {total}")
    escritor.add_metadata({"/Producer": "Unir PDF (local)"})
    salida = io.BytesIO()
    escritor.write(salida)
    return salida.getvalue(), len(escritor.pages)


def abrir_archivo(ruta: str):
    """Abre el archivo con el programa predeterminado del sistema."""
    if sys.platform == "win32":
        os.startfile(ruta)
    else:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", ruta], check=False)


def guardar_bytes(datos: bytes, destino: Path):
    temporal = destino.with_name(destino.name + ".tmp")
    try:
        temporal.write_bytes(datos)
        os.replace(temporal, destino)
    except PermissionError:
        temporal.unlink(missing_ok=True)
        raise PermissionError(f"Windows no dejó escribir {destino.name}. Si lo tienes abierto "
                              "en otro programa, ciérralo o guarda con otro nombre.") from None


def unir(elementos: list[Elemento], destino: Path, meta: int | None = None,
         grises: bool = False, avance: Callable[[str], None] | None = None) -> Resultado:
    """Une y guarda. meta = peso máximo en bytes (None: sin comprimir).

    Prueba los NIVELES de mejor a peor calidad y se queda con el primero que
    pese menos de la meta; si ninguno llega, con el más ligero.
    """
    original, hojas = construir(elementos, avance=avance)
    res = Resultado(hojas, len(original), len(original))
    datos = original
    if meta is not None and (len(original) > meta or grises):
        docs: dict = {}
        cache: dict = {}
        mejor: tuple[bytes, tuple[int, int]] | None = None
        try:
            for nivel in NIVELES:
                if any(llave[2] != nivel[0] for llave in cache):
                    cache.clear()  # cambió la resolución: soltar las hojas ya dibujadas
                intento, _ = construir(elementos, nivel, grises, docs, cache, avance)
                if mejor is None or len(intento) < len(mejor[0]):
                    mejor = (intento, nivel)
                if len(intento) <= meta:
                    break
        finally:
            for doc in docs.values():
                doc.close()
        if len(mejor[0]) < len(original) or grises:
            datos, res.nivel = mejor
        res.peso = len(datos)
        res.alcanzo_meta = len(datos) <= meta
    guardar_bytes(datos, destino)
    return res


def dibujar(doc: pdfium.PdfDocument, hoja: int, caja: tuple[int, int], giro: int) -> Image.Image:
    """Dibuja una hoja tal como quedará (con su giro), ajustada a la caja."""
    pagina = doc[hoja]
    ancho, alto = pagina.get_size()
    escala = max(caja) / max(ancho, alto)
    img = pagina.render(scale=escala).to_pil()
    if giro:
        img = img.rotate(-giro, expand=True)  # el giro de PDF es en sentido horario
    img.thumbnail(caja)
    return img


class DialogoHojas(tk.Toplevel):
    """Ventana para ordenar, girar y quitar hojas de un solo archivo."""

    def __init__(self, padre: tk.Tk, el: Elemento):
        super().__init__(padre)
        self.el = el
        self.orden = list(el.orden)
        self.giros = dict(el.giros_hoja)
        self.aceptado = False
        self.doc = pdfium.PdfDocument(el.datos, password=el.clave or None)
        self.minis: dict[tuple[int, int], ImageTk.PhotoImage] = {}  # (hoja, giro) -> imagen
        self.grande: ImageTk.PhotoImage | None = None
        self._vista_pendiente: str | None = None
        px = self.px = escalador(self)
        self.caja_mini = (px(MINI[0]), px(MINI[1]))

        self.title(f"Ordenar hojas: {el.ruta.name}")
        self.transient(padre)
        self.minsize(px(700), px(450))
        self.geometry(f"{min(px(1000), int(self.winfo_screenwidth() * 0.9))}x"
                      f"{min(px(720), int(self.winfo_screenheight() * 0.85))}")

        marco = ttk.Frame(self, padding=px(10))
        marco.pack(fill="both", expand=True)
        marco.columnconfigure(0, weight=1)
        marco.columnconfigure(3, weight=2)
        marco.rowconfigure(0, weight=1)

        estilo = ttk.Style(self)
        estilo.configure("Hojas.Treeview", rowheight=self.caja_mini[1] + px(8))
        self.tabla = ttk.Treeview(marco, columns=("giro",), show="tree headings",
                                  selectmode="extended", style="Hojas.Treeview")
        self.tabla.heading("#0", text="Hoja (orden final)")
        self.tabla.heading("giro", text="Giro")
        self.tabla.column("#0", width=px(190), minwidth=px(150))
        self.tabla.column("giro", width=px(50), anchor="center", stretch=False)
        self.tabla.grid(row=0, column=0, sticky="nsew")
        barra = ttk.Scrollbar(marco, orient="vertical", command=self.tabla.yview)
        barra.grid(row=0, column=1, sticky="ns")
        self.tabla.configure(yscrollcommand=barra.set)
        self.tabla.bind("<<TreeviewSelect>>", lambda e: self.mostrar())

        botones = ttk.Frame(marco, padding=(10, 0))
        botones.grid(row=0, column=2, sticky="n")
        for texto, accion in [
            ("Subir", lambda: self.mover(-1)),
            ("Bajar", lambda: self.mover(1)),
            ("Al principio", lambda: self.extremo(True)),
            ("Al final", lambda: self.extremo(False)),
            ("Girar 90°", self.girar),
            ("Quitar hoja", self.quitar),
            ("Invertir orden", self.invertir),
            ("Restablecer", self.restablecer),
        ]:
            ttk.Button(botones, text=texto, command=accion, width=15).pack(pady=3, fill="x")

        # La vista previa se ajusta al espacio disponible; el marco no crece con la imagen.
        self.marco_vista = ttk.Frame(marco, width=px(420), height=px(520))
        self.marco_vista.grid(row=0, column=3, sticky="nsew")
        self.marco_vista.pack_propagate(False)
        self.vista = ttk.Label(self.marco_vista, anchor="center")
        self.vista.pack(fill="both", expand=True)
        self.marco_vista.bind("<Configure>", lambda e: self.programar_vista())

        pie = ttk.Frame(marco, padding=(0, px(10), 0, 0))
        pie.grid(row=1, column=0, columnspan=4, sticky="ew")
        self.estado = tk.StringVar()
        ttk.Label(pie, textvariable=self.estado, foreground="#555").pack(side="left")
        ttk.Button(pie, text="Cancelar", command=self.cerrar).pack(side="right")
        ttk.Button(pie, text="Aceptar", command=self.aceptar).pack(side="right", padx=6)

        self.bind("<Delete>", lambda e: self.quitar())
        self.bind("<Alt-Up>", lambda e: self.mover(-1))
        self.bind("<Alt-Down>", lambda e: self.mover(1))
        self.bind("<Escape>", lambda e: self.cerrar())
        self.protocol("WM_DELETE_WINDOW", self.cerrar)

        self.refrescar([0])
        self.grab_set()

    def giro_de(self, hoja: int) -> int:
        return (self.el.giro + self.giros.get(hoja, 0)) % 360

    def mini(self, hoja: int) -> ImageTk.PhotoImage:
        llave = (hoja, self.giro_de(hoja))
        if llave not in self.minis:
            try:
                img = dibujar(self.doc, hoja, self.caja_mini, llave[1])
            except Exception:
                img = Image.new("RGB", self.caja_mini, "#dddddd")
            self.minis[llave] = ImageTk.PhotoImage(con_borde(img), master=self)
        return self.minis[llave]

    def seleccion(self) -> list[int]:
        return sorted(int(i) for i in self.tabla.selection())

    def refrescar(self, seleccionar: list[int] | None = None):
        self.config(cursor="watch")
        self.update_idletasks()
        self.tabla.delete(*self.tabla.get_children())
        for pos, hoja in enumerate(self.orden):
            texto = f"  {pos + 1}" + ("" if pos == hoja else f"   (era la {hoja + 1})")
            giro = self.giros.get(hoja, 0)
            self.tabla.insert("", "end", iid=str(pos), text=texto, image=self.mini(hoja),
                              values=(f"{giro}°" if giro else "",))
        self.config(cursor="")
        if seleccionar:
            seleccionar = [i for i in seleccionar if i < len(self.orden)] or [len(self.orden) - 1]
            self.tabla.selection_set([str(i) for i in seleccionar])
            self.tabla.see(str(seleccionar[0]))
        quitadas = self.el.total - len(self.orden)
        self.estado.set(f"{len(self.orden)} hojas" + (f" ({quitadas} quitadas)" if quitadas else ""))
        self.mostrar()

    def programar_vista(self):
        """Redibuja la vista previa al cambiar el tamaño, sin hacerlo en cada pixel."""
        if self._vista_pendiente:
            self.after_cancel(self._vista_pendiente)
        self._vista_pendiente = self.after(120, self.mostrar)

    def mostrar(self):
        self._vista_pendiente = None
        sel = self.seleccion()
        if not sel:
            self.vista.configure(image="", text="")
            return
        hoja = self.orden[sel[0]]
        caja = (max(self.marco_vista.winfo_width() - self.px(12), self.px(100)),
                max(self.marco_vista.winfo_height() - self.px(12), self.px(130)))
        try:
            self.grande = ImageTk.PhotoImage(
                con_borde(dibujar(self.doc, hoja, caja, self.giro_de(hoja))), master=self)
            self.vista.configure(image=self.grande, text="")
        except Exception as e:
            self.vista.configure(image="", text=f"Sin vista previa:\n{e}")

    def mover(self, paso: int):
        self.refrescar(mover_indices(self.orden, self.seleccion(), paso))

    def extremo(self, al_principio: bool):
        sel = self.seleccion()
        if not sel:
            return
        movidas = [self.orden[i] for i in sel]
        resto = [h for i, h in enumerate(self.orden) if i not in sel]
        self.orden = movidas + resto if al_principio else resto + movidas
        n = len(movidas)
        self.refrescar(list(range(n)) if al_principio else list(range(len(self.orden) - n, len(self.orden))))

    def girar(self):
        sel = self.seleccion()
        for i in sel:
            hoja = self.orden[i]
            self.giros[hoja] = (self.giros.get(hoja, 0) + 90) % 360
        self.refrescar(sel)

    def quitar(self):
        sel = self.seleccion()
        if not sel:
            return
        if len(sel) == len(self.orden):
            messagebox.showinfo("Quitar hoja", "El archivo se queda sin hojas. Para quitarlo "
                                "completo usa Quitar en la ventana principal.", parent=self)
            return
        for i in reversed(sel):
            del self.orden[i]
        self.refrescar([sel[0]])

    def invertir(self):
        self.orden.reverse()
        self.refrescar([0])

    def restablecer(self):
        self.orden = list(range(self.el.total))
        self.giros = {}
        self.refrescar([0])

    def aceptar(self):
        self.el.orden = self.orden
        self.el.giros_hoja = {h: g for h, g in self.giros.items() if g}
        self.aceptado = True
        self.cerrar()

    def cerrar(self):
        if self._vista_pendiente:
            self.after_cancel(self._vista_pendiente)
        self.grab_release()
        self.destroy()
        self.doc.close()


class DialogoGuardar(tk.Toplevel):
    """Pregunta si se guarda en calidad original o comprimido a un peso máximo."""

    NOTA = ("Las hojas escaneadas se vuelven a guardar como imagen más ligera; si tenían "
            "texto seleccionable, lo pierden. Las hojas de solo texto no se tocan. Se prueba "
            "de mayor a menor calidad hasta que el archivo pese menos de lo que pides.")

    def __init__(self, padre: tk.Tk, comprimir: bool, meta_mb: float, grises: bool):
        super().__init__(padre)
        self.title("Opciones del PDF")
        self.transient(padre)
        self.resizable(False, False)
        px = escalador(self)
        self.resultado: tuple[bool, float, bool] | None = None
        self.comprimir = tk.BooleanVar(value=comprimir)
        self.meta = tk.StringVar(value=f"{meta_mb:g}")
        self.grises = tk.BooleanVar(value=grises)

        m = ttk.Frame(self, padding=px(14))
        m.pack(fill="both", expand=True)
        ttk.Radiobutton(m, text="Calidad original (sin comprimir)", variable=self.comprimir,
                        value=False, command=self.actualizar).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(m, text="Comprimir para que pese menos de", variable=self.comprimir,
                        value=True, command=self.actualizar).grid(row=1, column=0, sticky="w", pady=(px(8), 0))
        self.caja = ttk.Spinbox(m, from_=0.2, to=100, increment=0.1, width=6, textvariable=self.meta)
        self.caja.grid(row=1, column=1, padx=px(6), pady=(px(8), 0))
        ttk.Label(m, text="MB").grid(row=1, column=2, sticky="w", pady=(px(8), 0))
        self.chk = ttk.Checkbutton(m, text="Escala de grises (ahorra más si las hojas tienen color)",
                                   variable=self.grises)
        self.chk.grid(row=2, column=0, columnspan=3, sticky="w", padx=(px(24), 0), pady=(px(6), 0))
        ttk.Label(m, text=self.NOTA, foreground="#555", wraplength=px(400), justify="left").grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(px(12), 0))
        pie = ttk.Frame(m)
        pie.grid(row=4, column=0, columnspan=3, sticky="e", pady=(px(14), 0))
        ttk.Button(pie, text="Continuar…", command=self.continuar).pack(side="left", padx=px(6))
        ttk.Button(pie, text="Cancelar", command=self.destroy).pack(side="left")

        self.bind("<Return>", lambda e: self.continuar())
        self.bind("<Escape>", lambda e: self.destroy())
        self.actualizar()
        self.grab_set()
        self.focus_set()

    def actualizar(self):
        estado = ["!disabled"] if self.comprimir.get() else ["disabled"]
        self.caja.state(estado)
        self.chk.state(estado)

    def continuar(self):
        mb = 0.0
        if self.comprimir.get():
            try:
                mb = float(self.meta.get().replace(",", "."))
                if mb <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Opciones del PDF", "Escribe un peso en MB, por ejemplo 1 o 0.8.",
                                     parent=self)
                return
        self.resultado = (self.comprimir.get(), mb, self.grises.get())
        self.destroy()


class App(tk.Tk):
    def __init__(self, iniciales: list[str]):
        super().__init__()
        px = escalador(self)
        self.title("Unir PDF (local)")
        self.geometry(f"{px(800)}x{px(480)}")
        self.minsize(px(620), px(360))
        self.elementos: list[Elemento] = []
        self.opciones = (True, 1.0, False)  # comprimir, meta en MB, escala de grises

        ttk.Style(self).configure("Treeview", rowheight=alto_renglon(self, px))

        marco = ttk.Frame(self, padding=px(10))
        marco.pack(fill="both", expand=True)
        marco.columnconfigure(0, weight=1)
        marco.rowconfigure(0, weight=1)

        self.tabla = ttk.Treeview(marco, columns=("hojas", "giro"),
                                  show="tree headings", selectmode="extended")
        self.tabla.heading("#0", text="Archivo (en este orden)")
        self.tabla.heading("hojas", text="Hojas")
        self.tabla.heading("giro", text="Giro")
        self.tabla.column("#0", width=px(300), minwidth=px(150))
        self.tabla.column("hojas", width=px(70), anchor="center", stretch=False)
        self.tabla.column("giro", width=px(60), anchor="center", stretch=False)
        self.tabla.grid(row=0, column=0, sticky="nsew")
        barra = ttk.Scrollbar(marco, orient="vertical", command=self.tabla.yview)
        barra.grid(row=0, column=1, sticky="ns")
        self.tabla.configure(yscrollcommand=barra.set)
        self.tabla.bind("<Double-1>", lambda e: self.ordenar_hojas())

        botones = ttk.Frame(marco, padding=(px(10), 0, 0, 0))
        botones.grid(row=0, column=2, sticky="n")
        for texto, accion in [
            ("Agregar…", self.agregar),
            ("Subir", lambda: self.mover(-1)),
            ("Bajar", lambda: self.mover(1)),
            ("Ordenar hojas…", self.ordenar_hojas),
            ("Girar 90°", self.girar),
            ("Quitar", self.quitar),
            ("Vaciar", self.vaciar),
        ]:
            ttk.Button(botones, text=texto, command=accion, width=16).pack(pady=3, fill="x")
        ttk.Separator(botones).pack(fill="x", pady=10)
        ttk.Button(botones, text="Unir y guardar…", command=self.guardar,
                   width=16).pack(pady=3, fill="x")

        self.estado = tk.StringVar()
        ttk.Label(marco, textvariable=self.estado, foreground="#555").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(px(8), 0))

        self.bind("<Delete>", lambda e: self.quitar())
        self.bind("<Alt-Up>", lambda e: self.mover(-1))
        self.bind("<Alt-Down>", lambda e: self.mover(1))

        self.refrescar()
        if iniciales:
            self.after(100, lambda: self.cargar_rutas([Path(r) for r in iniciales]))

    # --- lista ---
    def seleccion(self) -> list[int]:
        return sorted(int(i) for i in self.tabla.selection())

    def refrescar(self, seleccionar: list[int] | None = None):
        self.tabla.delete(*self.tabla.get_children())
        for i, el in enumerate(self.elementos):
            nombre = el.ruta.name + ("   (hojas editadas)" if el.editado else "")
            hojas = str(el.hojas) if el.hojas == el.total else f"{el.hojas} de {el.total}"
            self.tabla.insert("", "end", iid=str(i), text=nombre,
                              values=(hojas, f"{el.giro}°" if el.giro else ""))
        if seleccionar:
            self.tabla.selection_set([str(i) for i in seleccionar])
            self.tabla.see(str(seleccionar[0]))
        total = sum(el.hojas for el in self.elementos)
        self.estado.set(f"{len(self.elementos)} archivos, {total} hojas. Doble clic en un "
                        "archivo para ordenar sus hojas. Nada se sube a internet.")

    def pedir_clave(self, nombre: str) -> str | None:
        return simpledialog.askstring("Contraseña", f"{nombre} tiene contraseña:",
                                      show="*", parent=self)

    def cargar_rutas(self, rutas: list[Path]):
        fallas = []
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            for ruta in sorted(rutas, key=orden_natural):
                try:
                    self.elementos.append(cargar(ruta, self.pedir_clave))
                except Exception as e:  # un archivo malo no tumba a los demás
                    fallas.append(f"• {ruta.name}: {e}")
        finally:
            self.config(cursor="")
        self.refrescar()
        if fallas:
            messagebox.showwarning("No se pudieron agregar", "\n".join(fallas), parent=self)

    def agregar(self):
        tipos = " ".join(f"*{e}" for e in sorted(EXT_PDF | EXT_IMG))
        rutas = filedialog.askopenfilenames(
            parent=self, title="Elige los PDF o imágenes",
            filetypes=[("PDF e imágenes", tipos), ("Todos", "*.*")])
        if rutas:
            self.cargar_rutas([Path(r) for r in rutas])

    def mover(self, paso: int):
        self.refrescar(mover_indices(self.elementos, self.seleccion(), paso))

    def ordenar_hojas(self):
        sel = self.seleccion()
        if len(sel) != 1:
            messagebox.showinfo("Ordenar hojas", "Selecciona un solo archivo.", parent=self)
            return
        try:
            dialogo = DialogoHojas(self, self.elementos[sel[0]])
        except Exception as e:
            messagebox.showerror("Ordenar hojas", f"No se pudo abrir: {e}", parent=self)
            return
        self.wait_window(dialogo)
        self.refrescar(sel)

    def girar(self):
        sel = self.seleccion()
        for i in sel:
            self.elementos[i].giro = (self.elementos[i].giro + 90) % 360
        self.refrescar(sel)

    def quitar(self):
        sel = self.seleccion()
        for i in reversed(sel):
            del self.elementos[i]
        self.refrescar()

    def vaciar(self):
        if self.elementos and messagebox.askyesno("Vaciar", "¿Quitar todos los archivos?", parent=self):
            self.elementos.clear()
            self.refrescar()

    # --- guardar ---
    def guardar(self):
        if not self.elementos:
            messagebox.showinfo("Unir PDF", "Primero agrega archivos.", parent=self)
            return
        opciones = DialogoGuardar(self, *self.opciones)
        self.wait_window(opciones)
        if not opciones.resultado:
            return
        self.opciones = opciones.resultado
        comprimir, mb, grises = self.opciones
        destino = filedialog.asksaveasfilename(
            parent=self, title="Guardar PDF unido", defaultextension=".pdf",
            initialdir=str(self.elementos[0].ruta.parent), initialfile="unido.pdf",
            filetypes=[("PDF", "*.pdf")])
        if not destino:
            return

        def avance(texto: str):
            self.estado.set(texto + "…")
            self.update_idletasks()

        self.config(cursor="watch")
        self.update_idletasks()
        try:
            res = unir(self.elementos, Path(destino), meta=int(mb * 1_000_000) if comprimir else None,
                       grises=comprimir and grises, avance=avance)
        except Exception as e:
            messagebox.showerror("Error al guardar", str(e), parent=self)
            return
        finally:
            self.config(cursor="")
            self.refrescar(self.seleccion())

        texto = f"Se guardó con {res.hojas} hojas y pesa {tamano(res.peso)}."
        if res.nivel:
            texto += (f"\nSin comprimir pesaba {tamano(res.peso_original)}. "
                      f"Se usó {res.nivel[0]} ppp, calidad {res.nivel[1]}.")
        elif comprimir and res.alcanzo_meta:
            texto += "\nYa pesaba menos de lo pedido; no hizo falta comprimir."
        if comprimir and not res.alcanzo_meta:
            texto += (f"\n\nOjo: no se logró bajar de {mb:g} MB ni con la calidad más baja; "
                      "quedó el más ligero que se pudo.")
            if not grises:
                texto += " Puedes probar con escala de grises."
        if messagebox.askyesno("Listo", f"{texto}\n\n{destino}\n\n¿Abrirlo?", parent=self):
            abrir_archivo(destino)


def main():
    try:  # texto nítido en pantallas con escala de Windows
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App(sys.argv[1:]).mainloop()


if __name__ == "__main__":
    main()
