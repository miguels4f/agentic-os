"""Every Spanish string shown on the command-center page (rule D-S2-d).

The page is read by a non-engineer, so each technical term is explained in a short parenthesis
the first time it appears. ``render.py`` holds no Spanish text: it only arranges these strings
and escapes everything. Functions here return plain text; the caller escapes it.
"""

from __future__ import annotations

LANG = "es"
PAGE_TITLE = "Centro de mando de Wilfred"
PAGE_INTRO = (
    "Una foto de tus proyectos y de los trabajos de Wilfred en el momento indicado abajo. "
    "La página solo muestra: no cambia nada."
)
SOURCE_LINK = "fuente"
SOURCE_HELP = "Abre el archivo o la carpeta de donde sale este dato"
SOURCE_WEB_LINK = "en GitHub"
SOURCE_WEB_HELP = "Abre la copia de este archivo en GitHub (la copia en internet)"
FILTER_LABEL = "Filtrar trabajos y proyectos por texto"
FILTER_PLACEHOLDER = "Escribe un nombre de proyecto o de trabajo"
NONE = "ninguno"
UNKNOWN = "desconocido"
YES = "sí"
NO = "no"

# --- section titles, in page order ---
SECTION_SUMMARY = "Resumen"
SECTION_ATTENTION = "Necesita tu atención"
SECTION_RUNS = "Trabajos"
SECTION_PROJECTS = "Proyectos"
SECTION_MEMORY = "Memoria compartida"
SECTION_PROBLEMS = "Problemas de lectura"
SECTION_ORIGIN = "De dónde sale esta página"

# --- attention inbox (S2.4) ---
ATTENTION_INTRO = (
    "Lo que necesita a una persona, lo más urgente primero. La página no hace nada por ti: "
    "cada punto dice por qué hace falta alguien, qué se propone y qué pasa si nadie hace nada."
)


def attention_empty(local_time: str) -> str:
    return f"Nada necesita tu atención ahora mismo (foto del {local_time})."


def attention_count(count: int) -> str:
    return f"{count} {_plural(count, 'cosa necesita', 'cosas necesitan')} tu atención."


def attention_more(count: int) -> str:
    return f"y {count} más"


ATT_WHY = "Por qué hace falta alguien"
ATT_DEADLINE = "Plazo"
ATT_DEFAULT = "Propuesta"
ATT_IF_IGNORED = "Si nadie hace nada"
ATT_RISK = "Importancia"
RISK_LABELS = {"alto": "alta", "medio": "media", "bajo": "baja"}
# Each level is defined by something a non-engineer can check on the item itself.
RISK_MEANING = {
    "alto": (
        "hay una aprobación tuya pendiente (un plazo de aprobación abierto en Wilfred, o un "
        "cambio de ventas o una autorización de tarea que solo tú puedes aprobar) y está parado "
        "hasta que decidas"
    ),
    "medio": "algo está parado o puede perderse, pero no vence ningún plazo de aprobación",
    "bajo": "conviene ordenarlo; nada se para ni se pierde por esperar",
}
ATT_KIND_LABELS = {
    "gate": "permiso pedido",
    "awaiting_human": "espera decisión",
    "gate_failed": "comprobación fallida",
    "stale_run": "trabajo abandonado",
    "unpushed_work": "trabajo sin copia",
    "unreadable": "archivo ilegible",
    "memory_malformed": "nota incompleta",
    "ops_pending_proposal": "cambio por aprobar",
    "ops_overdue_task": "tarea vencida",
    "ops_blocked_task": "tarea bloqueada",
}


def _where(project: str | None, run_id: str) -> str:
    return f"{project}, trabajo {run_id}" if project else f"trabajo {run_id}"


def _actions(actions: list[str]) -> str:
    return ", ".join(f"«{a}»" for a in actions)


def att_gate_title(project: str | None, run_id: str, actions: list[str]) -> str:
    return f"{_where(project, run_id)}: Wilfred pide tu permiso para {_actions(actions)}."


def att_gate_why(actions: list[str], gate_ids: list[str]) -> str:
    return (
        "Wilfred se ha parado en una puerta (gate: un punto donde la máquina no puede darse "
        f"permiso a sí misma) y necesita que una persona apruebe {_actions(actions)} "
        f"(puerta {', '.join(gate_ids)})."
    )


def att_gate_default(deadline: str | None) -> str:
    when = f" antes del {deadline}" if deadline else ""
    return (
        "Pide a una sesión de Claude que te resuma lo que Wilfred propone y te prepare la orden "
        f"para aprobarlo o rechazarlo{when}; si la aprobación pide tu firma, la haces tú en tu "
        "Terminal."
    )


ATT_GATE_NO_WINDOW = (
    "No se encontró un plazo de aprobación abierto, así que hoy no vence nada; para decidir, "
    "probablemente haya que pedir a Wilfred que vuelva a abrir la petición."
)


def att_gate_ignored(deadline: str | None) -> str:
    if deadline:
        return (
            f"El {deadline} caduca el plazo: el trabajo sigue parado y habrá que pedir a Wilfred "
            "que vuelva a abrir la puerta antes de poder decidir."
        )
    return "El trabajo sigue parado hasta que alguien decida."


def att_gate_expired_title(project: str | None, run_id: str, actions: list[str]) -> str:
    return (
        f"{_where(project, run_id)}: el permiso que pedía Wilfred para {_actions(actions)} "
        "caducó sin respuesta."
    )


def att_gate_expired_why(expired_at: str | None) -> str:
    when = f" el {expired_at}" if expired_at else ""
    return (
        f"Solo una persona puede aprobarlo, y el plazo terminó{when} sin decisión; la petición "
        "ya no vale y hay que volver a pedirla."
    )


ATT_GATE_EXPIRED_DEFAULT = (
    "Pide a una sesión de Claude que vuelva a abrir la petición de permiso en este trabajo y "
    "decide entonces; si el trabajo ya no interesa, pide que se descarte."
)


def att_gate_expired_ignored(expired_at: str | None) -> str:
    since = f" desde el {expired_at}" if expired_at else ""
    return (
        f"El trabajo sigue parado{since} y no avanzará solo; lo que dependa de él tampoco."
    )


def att_awaiting_title(project: str | None, run_id: str) -> str:
    return f"{_where(project, run_id)}: Wilfred espera tu decisión para seguir."


ATT_AWAITING_WHY = (
    "Wilfred se paró a propósito porque hay una elección que no le corresponde a la máquina "
    "(por ejemplo, aceptar un resultado o cambiar el alcance)."
)
ATT_AWAITING_DEFAULT = (
    "Pide a una sesión de Claude que te resuma la pregunta de Wilfred en este trabajo (el "
    "enlace «fuente» lleva a su carpeta) y dile si continúa o si lo descartas."
)


def att_awaiting_ignored(since: str | None, days: int | None) -> str:
    if since is None or days is None:
        return (
            "El trabajo seguirá parado, y lo que dependa de él tampoco avanzará; no se sabe "
            "desde cuándo."
        )
    return (
        f"El trabajo seguirá parado, y lo que dependa de él tampoco avanzará; lleva así desde "
        f"el {since} (hace {days} {_plural(days, 'día', 'días')})."
    )


def att_gate_failed_title(project: str | None, run_id: str) -> str:
    return f"{_where(project, run_id)}: se paró porque no pasó una comprobación automática."


def att_gate_failed_why(step: str | None) -> str:
    where = f" en el paso «{step}»" if step else ""
    return (
        f"Las pruebas o revisiones automáticas fallaron{where}. Si merece la pena corregirlo o "
        "es mejor abandonarlo es una decisión de negocio, no de la máquina."
    )


ATT_GATE_FAILED_DEFAULT = (
    "Pide a una sesión de Claude que te diga qué comprobación falló y cuánto costaría "
    "arreglarlo; con eso, decide si se corrige y se reintenta o si se descarta."
)


def att_gate_failed_ignored(leaves: str | None, days: int) -> str:
    if leaves is None:
        return "Nada avanza. No se sabe cuándo se paró, así que el aviso seguirá aquí."
    return (
        f"Nada avanza. El {leaves} este aviso deja de aparecer aquí ({days} días después de "
        "pararse) y nadie volverá a avisarte, aunque el trabajo siga sin terminar."
    )


def att_stale_title(project: str | None, run_id: str, days: int) -> str:
    return f"{_where(project, run_id)}: lleva {days} días sin actividad."


def att_stale_why(since: str) -> str:
    return (
        f"Figura como «en curso», pero nada se mueve desde el {since}: probablemente se quedó a "
        "medias. Solo tú sabes si todavía interesa."
    )


ATT_STALE_DEFAULT = (
    "Si ya no interesa, pide a una sesión de Claude que lo descarte; si interesa, pídele que "
    "lo retome con Wilfred."
)
ATT_STALE_IGNORED = (
    "Seguirá contando como «en curso», así que la cifra de trabajos en marcha será engañosa."
)


def att_unpushed_title(project: str) -> str:
    return f"{project}: hay trabajo que solo existe en este Mac."


def att_unpushed_why(reasons: list[str]) -> str:
    return (
        "; ".join(reasons)
        + ". Decidir qué se guarda y qué se tira exige saber qué es cada cambio."
    )


def att_ahead(count: int) -> str:
    return (
        f"{count} {_plural(count, 'commit', 'commits')} (cambios ya guardados) sin subir a la "
        "copia en internet"
    )


def att_branches(names: list[str]) -> str:
    count = len(names)
    return (
        f"{count} {_plural(count, 'rama', 'ramas')} (versiones paralelas del código) solo en "
        "este Mac, sin copia en internet: " + ", ".join(names)
    )


def att_uncommitted(
    count: int, last_commit: str | None, days: int | None, *, has_commits: bool = True
) -> str:
    text = (
        f"{count} {_plural(count, 'archivo cambiado', 'archivos cambiados')} sin guardar en un "
        "commit (una foto guardada de los cambios)"
    )
    if not has_commits:
        return text + "; el proyecto no tiene ningún commit todavía"
    if last_commit is None or days is None:
        return text + "; no se pudo leer la fecha del último commit"
    return text + f"; el último commit es del {last_commit} (hace {days} días)"


ATT_UNPUSHED_DEFAULT = (
    "Pide a una sesión de Claude que guarde y suba ese trabajo a GitHub (la copia en internet), "
    "o que te diga qué es para que confirmes que se puede descartar."
)
ATT_UNPUSHED_IGNORED = (
    "Si el disco del Mac falla o alguien borra la carpeta, ese trabajo se pierde: no hay otra "
    "copia."
)


def att_unreadable_title(name: str) -> str:
    return f"No se pudo leer el archivo «{name}»."


def att_unreadable_why(what: str, reason: str) -> str:
    return (
        f"Motivo técnico, tal cual (en inglés): {what}: {reason}. Puede ser un archivo dañado o "
        "de una versión antigua de Wilfred; una persona debe decidir si se repara o se retira."
    )


ATT_UNREADABLE_DEFAULT = (
    "Pide a una sesión de Claude que mire el archivo (enlace «fuente») y lo repare o lo retire."
)
ATT_UNREADABLE_IGNORED = (
    "La página seguirá sin contar ese dato: las cifras de arriba pueden quedarse cortas y "
    "podría haber un aviso que no estás viendo."
)

_NOTE_FIELDS = {
    "name": "nombre",
    "description": "descripción",
    "type": "tipo (de qué clase de nota se trata)",
    "a recognised type": "un tipo reconocido",
}


def att_memory_title(name: str) -> str:
    return (
        f"La nota de memoria «{name}» (un apunte que los asistentes leen para recordar lo "
        "aprendido) está incompleta."
    )


def att_memory_why(problem: str) -> str:
    if problem.startswith("missing "):
        parts = [p.strip() for p in problem.removeprefix("missing ").split(",")]
        detail = (
            "A su ficha de datos (las líneas del principio) le falta: "
            + ", ".join(_NOTE_FIELDS.get(p, p) for p in parts)
        )
    elif problem == "no frontmatter block":
        detail = "No tiene ficha de datos al principio (nombre, descripción y tipo)"
    elif problem == "unreadable":
        detail = "No se pudo abrir"
    else:
        detail = f"Tiene un problema (motivo técnico, en inglés: {problem})"
    return (
        f"{detail}. Claude y los demás asistentes usan esos datos para encontrarla; "
        "completarla requiere saber de qué trata."
    )


ATT_MEMORY_DEFAULT = "Pide a una sesión de Claude que complete la ficha de datos de la nota."
ATT_MEMORY_IGNORED = (
    "Los asistentes pueden no encontrar esa nota y repetir algo que ya se aprendió o se corrigió."
)


# --- ops: tasks and sales kept in the ops repository (it replaced Notion) ---
OPS_TASK_STATUS = {
    "Not Started": "sin empezar",
    "In Progress": "en curso",
    "Blocked": "bloqueada",
}
SALES_FIELD_LABELS = {
    "company": "nombre de la empresa",
    "status": "estado del contacto",
    "segment": "segmento",
    "product": "producto",
    "priority": "prioridad",
    "country": "país",
    "next_action": "próxima acción",
    "next_action_date": "fecha de la próxima acción",
    "last_contacted": "fecha del último contacto",
    "meeting_date": "fecha de la reunión",
    "docs_generated": "documentos preparados",
    "contact_name": "nombre del contacto",
    "contact_role": "cargo del contacto",
    "contact_email": "email del contacto",
    "contact_linkedin": "LinkedIn del contacto",
    "pain_point": "problema que quiere resolver",
    "est_ticket": "importe estimado",
    "competitor_mentioned": "competidor mencionado",
    "loi_probability": "probabilidad de firmar una carta de intenciones",
    "duplicate_of": "duplicado de",
    "pilot_account": "cuenta del piloto",
}


def _sales_fields(names: list[str]) -> str:
    return ", ".join(f"«{SALES_FIELD_LABELS.get(name, name)}»" for name in names)


def att_ops_proposal_title(proposal_id: str, kind: str, name: str, fields: list[str]) -> str:
    if kind == "set":
        what = f"cambiar {_sales_fields(fields) or 'datos'} de «{name}»"
    elif kind == "append":
        what = f"añadir una nota a la ficha de «{name}»"
    elif kind == "create":
        what = f"dar de alta la empresa «{name}»"
    elif kind == "schema-option":
        what = (
            "añadir una opción nueva a la lista de valores permitidos del campo "
            f"{_sales_fields(fields) or 'de ventas'}"
        )
    else:
        what = f"un cambio de tipo «{kind}» en «{name}»"
    return f"Ventas: la propuesta {proposal_id} espera tu aprobación para {what}."


def att_ops_proposal_why(author: str | None, created: str | None) -> str:
    who = f"«{author}»" if author else "un asistente"
    when = f" el {created}" if created else ""
    return (
        f"Lo propuso {who}{when}. Los datos de ventas (las fichas de empresas y contactos) "
        "solo cambian con tu aprobación; ninguna máquina puede aprobarlo por ti."
    )


def att_ops_proposal_default(proposal_id: str) -> str:
    return (
        "Pide a una sesión de Claude que te enseñe el cambio exacto (esta página no copia los "
        "valores, por privacidad). Si estás de acuerdo, "
        f"apruébalo tú en tu Terminal con «ops approve {proposal_id}»; si no, recházalo con "
        f"«ops reject {proposal_id}»."
    )


ATT_OPS_PROPOSAL_IGNORED = (
    "El cambio no se aplica: los datos de ventas siguen como estaban y la propuesta seguirá "
    "aquí. Si es el cambio de un dato y ese dato cambia antes por otra vía, la propuesta deja "
    "de valer y habrá que hacerla de nuevo."
)


def att_ops_unapproved_title(name: str) -> str:
    return f"Ventas: hay un cambio hecho a mano en «{name}» que nadie ha aprobado."


ATT_OPS_UNAPPROVED_WHY = (
    "Alguien editó directamente un archivo de ventas (o la lista de valores permitidos). Esos "
    "cambios solo se guardan en GitHub (la copia en internet) cuando tú los apruebas; la "
    "sincronización automática no los sube."
)


def att_ops_unapproved_default(path: str) -> str:
    return (
        "Pide a una sesión de Claude que te enseñe qué cambió. Si es correcto, apruébalo tú en "
        f"tu Terminal con «ops approve --file {path}»; si no, pide que lo deshaga."
    )


ATT_OPS_UNAPPROVED_IGNORED = (
    "El cambio se queda solo en este Mac y no llega a la copia en internet; si el disco falla "
    "o alguien borra la carpeta, se pierde."
)


def att_ops_authorization_title(path: str) -> str:
    return (
        f"Tareas: autorización escrita a mano en {path}: revísala antes de aprobarla con "
        f"ops approve --file {path}."
    )


ATT_OPS_AUTHORIZATION_WHY = (
    "Alguien escribió a mano en el archivo de esta tarea quién la autorizó y cuándo. Esa "
    "autorización permite que un asistente haga la tarea sin preguntarte, así que solo vale si "
    "la apruebas tú; la sincronización automática no la guarda."
)


def att_ops_authorization_default(path: str) -> str:
    return (
        "Pide a una sesión de Claude que te enseñe qué cambió. Si la autorización es tuya, "
        f"apruébala tú en tu Terminal con «ops approve --file {path}»; si no, pide que la "
        "deshaga."
    )


ATT_OPS_AUTHORIZATION_IGNORED = (
    "La autorización no cuenta: se queda solo en este Mac, no llega a GitHub (la copia en "
    "internet) y ningún asistente hará la tarea basándose en ella."
)


def _ops_task(project: str | None, task_id: str, title: str, rest: str) -> str:
    named = f" «{title}»" if title else ""
    text = f"la tarea {task_id}{named} {rest}"
    return f"{project}: {text}" if project else text[:1].upper() + text[1:]


def att_ops_overdue_title(project: str | None, task_id: str, title: str, days: int) -> str:
    late = f"venció hace {days} {_plural(days, 'día', 'días')}."
    return _ops_task(project, task_id, title, late)


def att_ops_overdue_why(due: str, status: str, blocked_by: str | None) -> str:
    text = (
        f"Tenía fecha límite el {due} y sigue sin terminar (estado: {status}). Hacerla ya, "
        "darle otra fecha o aplazarla es una decisión tuya, no de la máquina."
    )
    if blocked_by:
        text += f" Además está bloqueada por: «{blocked_by}»."
    return text


ATT_OPS_OVERDUE_DEFAULT = (
    "Pide a una sesión de Claude que te diga en qué punto está la tarea y qué hace falta decidir "
    "y, según lo que "
    "decidas, que le ponga una fecha nueva o la marque como aplazada."
)
ATT_OPS_OVERDUE_IGNORED = (
    "La fecha seguirá pasada: la lista de tareas dirá que va con retraso, lo que dependa de "
    "ella también puede retrasarse, y nadie te avisará de otra forma."
)


def att_ops_blocked_title(project: str | None, task_id: str, title: str) -> str:
    return _ops_task(project, task_id, title, "está bloqueada.")


def att_ops_blocked_why(blocked_by: str | None) -> str:
    if blocked_by:
        return (
            f"Está parada por: «{blocked_by}». Quitar ese bloqueo suele pedir una decisión o "
            "una gestión que la máquina no puede hacer."
        )
    return (
        "Está parada y no dice por qué. Hace falta alguien que sepa qué la bloquea para "
        "anotarlo o resolverlo."
    )


ATT_OPS_BLOCKED_DEFAULT = (
    "Pide a una sesión de Claude que te resuma qué falta para desbloquearla; si ya no "
    "interesa, que la marque como aplazada."
)
ATT_OPS_BLOCKED_IGNORED = "La tarea seguirá parada. No vence ningún plazo, pero no avanzará sola."


# --- staleness ---
def stale_warning(hours: int) -> str:
    return (
        f"Atención: esta foto tiene {hours} horas. Lo que ves puede haber cambiado. "
        "Vuelve a generarla con la orden del final de la página."
    )


STALE_VIEWED = (
    "Atención: esta foto tiene más de 24 horas. Lo que ves puede haber cambiado. "
    "Vuelve a generarla con la orden del final de la página."
)
STALE_UNKNOWN = (
    "Atención: no se puede saber cuándo se hizo esta foto. Vuelve a generarla con la orden "
    "del final de la página."
)


def generated_line(local_time: str) -> str:
    return f"Foto tomada el {local_time} (hora de este Mac)."


# --- summary ---
def summary_repositories(repositories: int, worktrees: int, folders: int) -> str:
    text = (
        f"{repositories} {_plural(repositories, 'repositorio', 'repositorios')} "
        "(proyectos con historial de cambios)"
    )
    if worktrees:
        text += (
            f" y {worktrees} {_plural(worktrees, 'copia de trabajo', 'copias de trabajo')} "
            "(worktree: una segunda carpeta del mismo proyecto para trabajar en paralelo)"
        )
    if folders:
        text += f"; {folders} {_plural(folders, 'carpeta sin historial', 'carpetas sin historial')}"
    return text + "."


def summary_unfinished(count: int, names: list[str] | None = None) -> str:
    if count == 0:
        return "Ningún proyecto tiene trabajo sin guardar ni sin subir."
    text = (
        f"{count} {_plural(count, 'carpeta tiene', 'carpetas tienen')} trabajo sin terminar, "
        "contando repositorios y copias de trabajo (cambios que solo existen en este Mac: "
        "si el Mac falla, se pierden)"
    )
    if names:
        text += ": " + _some(names)
    return text + ". Están primero en «Proyectos»."


def summary_git_unknown(count: int) -> str:
    return (
        f"De {count} {_plural(count, 'carpeta', 'carpetas')} no se pudo saber si hay trabajo "
        "sin guardar (git no respondió); no cuentes con que estén al día."
    )


def summary_runs(distinct: int, copies: int) -> str:
    text = (
        f"{distinct} {_plural(distinct, 'trabajo', 'trabajos')} de Wilfred "
        "(run: cada encargo que Wilfred ejecuta sobre un proyecto)"
    )
    if copies > distinct:
        text += (
            f"; {copies - distinct} "
            f"{_plural(copies - distinct, 'copia repetida', 'copias repetidas')}"
            " en otras carpetas, que se muestran una sola vez"
        )
    return text + "."


def children_unfinished(count: int) -> str:
    return f"{count} con trabajo sin terminar"


QUOTE_OPEN = "«"
QUOTE_CLOSE = "»"


def summary_gates(runs: int, gates: int, names: list[str] | None = None) -> str:
    if gates == 0:
        return (
            "Ninguna puerta abierta (gate: punto donde Wilfred se para hasta que una persona "
            "aprueba)."
        )
    return (
        f"{gates} {_plural(gates, 'puerta abierta', 'puertas abiertas')} en {runs} "
        f"{_plural(runs, 'trabajo', 'trabajos')} (gate: punto donde Wilfred se para hasta que "
        "una persona aprueba). "
        + (f"Esperan tu decisión: {_some(names)}. " if names else "")
        + "No avanzan hasta que decidas; el detalle está en «Trabajos»."
    )


def summary_cost(recorded: int, distinct: int) -> str:
    if recorded == 0:
        return (
            "Coste: desconocido en todos los trabajos (no se registró el consumo). "
            "No significa que fuera gratis."
        )
    return f"Coste registrado en {recorded} de {distinct} trabajos; en el resto es desconocido."


def summary_problems(count: int) -> str:
    if count == 0:
        return "Se pudo leer todo."
    return (
        f"{count} {_plural(count, 'archivo no se pudo leer', 'archivos no se pudieron leer')}; "
        "la lista está en «Problemas de lectura». Un trabajo cuyo estado no se pudo leer "
        "aparece en el grupo «No legible»."
    )


def summary_memory(notes: int | None) -> str:
    if notes is None:
        return "Memoria compartida: no se pudo leer."
    return f"Memoria compartida: {notes} {_plural(notes, 'nota', 'notas')}."


# --- run groups (stored status -> Spanish label), in page order ---
GROUP_AWAITING = "Esperando decisión humana"
GROUP_GATE_FAILED = "Bloqueado por una comprobación fallida"
GROUP_IN_PROGRESS = "En curso"
GROUP_COMPLETED = "Terminado"
GROUP_FAILED = "Falló"
GROUP_CREATED = "Creado, sin empezar"
GROUP_OTHER = "Estado no reconocido"
GROUP_UNREADABLE = "No legible"
GROUP_CLOSED = "Cerrados"

GROUP_WHY = {
    "awaiting_human": "Wilfred está parado hasta que una persona apruebe o rechace. "
    "Si nadie decide, el trabajo sigue parado.",
    "gate_failed": "Una comprobación automática (pruebas, revisión o seguridad) no pasó y el "
    "trabajo se detuvo. No avanza solo: hay que pedir a Wilfred que lo corrija o "
    "decidir descartarlo.",
    "in_progress": "Empezados y sin terminar. Los que llevan mucho tiempo sin actividad "
    "lo indican: probablemente se quedaron a medias y conviene retomarlos o descartarlos.",
    "completed": "Acabados. No necesitan nada.",
    "failed": "Se detuvieron por un error.",
    "created": "Preparados pero sin empezar.",
    "other": "Wilfred guardó un estado que esta página no reconoce; se muestra tal cual.",
    "unreadable": "Su archivo de estado está dañado o no se pudo abrir. "
    "Mira «Problemas de lectura».",
    "closed": "Trabajos que la persona responsable dio por cerrados en el registro de cierre "
    "(closed-runs.json, junto a esta página). No piden atención y no se han movido: se "
    "conservan como evidencia o historial. Si uno tiene una aprobación con plazo abierto, "
    "sigue apareciendo en «Necesita tu atención».",
}
GROUP_EMPTY = "Ninguno en este grupo."
GROUP_LABELS = {
    "awaiting_human": GROUP_AWAITING,
    "gate_failed": GROUP_GATE_FAILED,
    "in_progress": GROUP_IN_PROGRESS,
    "completed": GROUP_COMPLETED,
    "failed": GROUP_FAILED,
    "created": GROUP_CREATED,
    "other": GROUP_OTHER,
    "unreadable": GROUP_UNREADABLE,
    "closed": GROUP_CLOSED,
}
COST_UNKNOWN_SOURCE_HELP = (
    "Carpeta del trabajo: no contiene un registro de consumo (usage.json)"
)


def run_title(run_id: str, project: str | None) -> str:
    return f"{run_id} — proyecto {project}" if project else run_id


RUN_MODE = "Tipo de encargo"
MODE_NAMES = {"build": "construir algo nuevo", "improve": "mejorar algo existente",
              "review": "revisar código"}
RUN_STEP = "Paso actual"
RUN_STEP_NONE = "todos los pasos terminados"
STEP_LABELS = {
    "frame": "plantear el problema",
    "decompose": "dividir en tareas",
    "scaffold": "preparar la estructura",
    "comprehend": "entender el código",
    "locate": "localizar dónde cambiar",
    "implement": "programar el cambio",
    "regression": "comprobar que nada se rompió",
    "quality": "control de calidad",
    "verify": "verificación final",
    "critique": "revisión crítica",
    "evaluate": "evaluación",
    "report": "informe",
    "ship": "entregar (unir el cambio al proyecto)",
}
STEP_NAMES = {"pending": "pendiente", "running": "en marcha", "done": "hecho", "failed": "falló"}
RUN_UPDATED = "Última actividad"
RUN_LAST_MESSAGE = "Último mensaje de Wilfred (tal cual lo escribió, en inglés)"
RUN_NO_EVENTS = "sin mensajes registrados"
RUN_GATE = "Puerta abierta"
RUN_QUALITY = "Comprobaciones de calidad"
RUN_DUPLICATE = "también presente en"
RUN_STORE = "Carpeta del trabajo"
RUN_CLOSED = "Cerrado"


def closed_line(reason: str, closed_on: str | None) -> str:
    return f"{reason} (el {closed_on})" if closed_on else reason


def closed_summary(count: int) -> str:
    return f"Ver {count} {_plural(count, 'trabajo cerrado', 'trabajos cerrados')}"
COST_LABEL = "Coste"
COST_UNKNOWN = "Coste: desconocido (no se registró el consumo)"


def gate_line(gate_id: str, action: str | None, expired: bool | None) -> str:
    text = f"Wilfred pide permiso para: {action}" if action else "Wilfred pide permiso"
    text += f" (puerta «{gate_id}»)."
    if expired is True:
        text += (
            " Tu acción: el plazo para aprobarla caducó, así que primero hay que pedir a Wilfred "
            "que la vuelva a abrir; después, aprobar o rechazar."
        )
    elif expired is False:
        text += " Tu acción: aprobar o rechazar; está dentro de plazo."
    else:
        text += " Tu acción: aprobar o rechazar."
    return text


def quality_line(passed: bool | None, iteration: int | None, maximum: int | None) -> str:
    if passed is True:
        result = "las comprobaciones automáticas (pruebas y revisiones) pasaron"
    elif passed is False:
        result = "las comprobaciones automáticas (pruebas y revisiones) no pasaron"
    else:
        result = "sin resultado todavía"
    if iteration is not None and maximum is not None:
        return f"{result} (intento {iteration} de {maximum} permitidos)"
    return result


def cost_recorded(tokens: dict[str, int]) -> str:
    total = sum(tokens.values())
    return f"Coste: {total} fichas (tokens: unidades de texto que cobra el modelo) registradas"


def inactive(days: int) -> str:
    return (
        f"Sin actividad desde hace {days} días: probablemente se quedó a medias. "
        "Conviene retomarlo o descartarlo."
    )


def age(days: int, hours: int) -> str:
    if days >= 1:
        return f"hace {days} {_plural(days, 'día', 'días')}"
    if hours >= 1:
        return f"hace {hours} {_plural(hours, 'hora', 'horas')}"
    return "hace menos de una hora"


def until(days: int, hours: int) -> str:
    if days >= 1:
        left = f"{days} {_plural(days, 'día', 'días')}"
    elif hours >= 1:
        left = f"{hours} {_plural(hours, 'hora', 'horas')}"
    else:
        left = "menos de una hora"
    return f"quedaban {left} al generar la página"


DEADLINE_PASSED = "el plazo ya pasó"


# --- projects ---
PROJECTS_INTRO = (
    "Cada proyecto muestra su rama (branch: la línea de trabajo activa) y su último commit "
    "(una foto guardada de los cambios). Los que tienen trabajo sin terminar van primero."
)
PROJECT_UNFINISHED = "Trabajo sin terminar"
PROJECT_CLEAN = "Todo guardado y subido"
PROJECT_BRANCH = "Rama"
PROJECT_DETACHED = "ninguna (está fijado en un commit concreto, no en una rama)"
PROJECT_NO_COMMITS = "sin commits todavía"
PROJECT_LAST_COMMIT = "Último commit"
PROJECT_REMOTES = "Copia en internet (remoto)"
PROJECT_NO_REMOTES = "ninguna: el trabajo solo existe en este Mac"
PROJECT_UNKNOWN_STATE = "no se pudo leer el estado con git; mira «Problemas de lectura»"
PROJECT_PARTIAL = (
    "No se pudo saber con git si todo está guardado y subido; mira «Problemas de lectura»"
)
PROJECT_FOLDER = "Carpeta sin historial de cambios (no es un repositorio)"


def uncommitted(count: int) -> str:
    return (
        f"{count} {_plural(count, 'archivo cambiado', 'archivos cambiados')} sin guardar en un "
        "commit"
    )


def ahead(count: int) -> str:
    return f"{count} {_plural(count, 'commit', 'commits')} sin subir a la copia en internet"


def behind(count: int) -> str:
    return (
        f"{count} {_plural(count, 'commit', 'commits')} en la copia en internet que este Mac "
        "aún no tiene"
    )


def branches_without_upstream(names: list[str]) -> str:
    count = len(names)
    return (
        f"{count} {_plural(count, 'rama', 'ramas')} solo en este Mac, sin copia en internet: "
        + ", ".join(names)
    )


def worktrees_summary(count: int) -> str:
    return (
        f"{count} {_plural(count, 'copia de trabajo', 'copias de trabajo')} (worktree) de este "
        "proyecto"
    )


def run_store_worktrees(count: int) -> str:
    return (
        f"{count} {_plural(count, 'copia de trabajo interna', 'copias de trabajo internas')} "
        "de Wilfred (las crea él mismo dentro de un trabajo; no suelen necesitar nada)"
    )


WORKTREE_MISSING = "la carpeta ya no existe"
WORKTREE_PRUNABLE = "git la marca para limpiar"
WORKTREE_OF = "copia de trabajo de"

# --- memory ---
MEMORY_INTRO = (
    "Las notas que comparten Claude, Codex y Cursor para recordar cosas entre sesiones."
)
MEMORY_ROOT = "Carpeta"
MEMORY_NOTES = "Notas"
MEMORY_TYPES = {
    "project": "de proyecto",
    "feedback": "correcciones tuyas",
    "user": "sobre ti",
    "reference": "referencias",
    "untyped": "sin tipo",
}
MEMORY_INDEX = "Líneas del índice (MEMORY.md)"
MEMORY_INDEX_UNKNOWN = "no se pudo leer el índice"
MEMORY_MALFORMED = (
    "Notas mal formadas (les falta nombre, descripción o tipo; no se encuentran bien)"
)
MEMORY_MISSING = "No se leyó la memoria compartida."

# --- problems ---
PROBLEMS_NONE = "Ninguno: se pudo leer todo."
PROBLEMS_INTRO = (
    "Cosas que no se pudieron leer. No cuentan en las cifras de arriba, así que la foto "
    "puede estar incompleta en esos puntos."
)

# --- origin ---
ORIGIN_GENERATED = "Generada"
ORIGIN_WORKSPACE = "Carpeta revisada"
ORIGIN_COMMIT = "Versión del programa (commit)"
ORIGIN_COMMIT_UNKNOWN = "desconocida (programa instalado como paquete)"
ORIGIN_VERSION = "Versión de Wilfred"
ORIGIN_SNAPSHOT = "Datos en bruto"
ORIGIN_READ_ONLY = (
    "Esta página solo lee: no cambia, crea ni borra nada en tus proyectos ni en la memoria. "
    "Cada dato lleva un enlace «fuente» al archivo o a la orden de git de la que sale."
)
ORIGIN_REFRESH = "Para actualizarla, ejecuta en la Terminal:"


def _some(names: list[str], limit: int = 5) -> str:
    shown = ", ".join(names[:limit])
    if len(names) > limit:
        shown += f" y {len(names) - limit} más"
    return shown


def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many
