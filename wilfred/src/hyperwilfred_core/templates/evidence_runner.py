"""Standalone generated agent; the trusted host brokers model calls over JSON lines."""
import json
import sys

FAMILY = "__FAMILY__"
trace = []


def block(why, unlock="Supply bounded evidence or review the model response"):
    print(json.dumps({"accepted": False, "error": {"why": why, "unlock": unlock}, "trace": trace}))
    raise SystemExit(2)


def model(role, context):
    print(json.dumps({"model_request": {"role": role, "context": context}}), flush=True)
    reply = json.loads(sys.stdin.readline())
    trace.append({"actor": role, "event": "model", "action": reply.get("action")})
    return reply


def compose(role, question, documents, feedback=None):
    context = {"question": question, "available_ids": [d["id"] for d in documents],
               "tool": "lookup: select a word or document id; finish with retrieved citations"}
    if feedback:
        context["revision"] = feedback
    retrieved = {}
    for _ in range(5):
        response = model(role, context)
        if response.get("action") == "lookup":
            query = response.get("query", "")
            if not isinstance(query, str) or not query.strip() or len(query) > 120:
                block("invalid bounded lookup query")
            matches = [d for d in documents if query.casefold() in
                       (d["id"] + " " + d["text"]).casefold()][:5]
            retrieved.update((d["id"], d) for d in matches)
            context["lookup_results"] = list(retrieved.values())
            context["last_query"] = query
            trace.append({"actor": role, "event": "lookup", "query": query,
                          "ids": [d["id"] for d in matches]})
        elif response.get("action") == "finish":
            answer, citations = response.get("answer"), response.get("citations")
            if (not isinstance(answer, str) or not answer.strip() or len(answer) > 4000
                    or not isinstance(citations, list) or not citations
                    or any(not isinstance(c, str) or c not in retrieved for c in citations)):
                block("answer must cite retrieved evidence")
            return {"answer": answer, "citations": citations}
        else:
            block("model selected an unavailable operation")
    block("model exceeded five reasoning/tool turns")


try:
    payload = json.loads(sys.stdin.readline())
except (ValueError, UnicodeDecodeError):
    block("input is not valid JSON")
if not isinstance(payload, dict):
    block("input must be an object")
question, documents = payload.get("question"), payload.get("documents")
if (not isinstance(question, str) or not question.strip() or len(question) > 2000
        or not isinstance(documents, list) or len(documents) > 20):
    block("provide a bounded question and documents list")
if any(not isinstance(d, dict) or not isinstance(d.get("id"), str) or not d["id"]
       or len(d["id"]) > 80 or not isinstance(d.get("text"), str) or len(d["text"]) > 8000
       for d in documents):
    block("documents require bounded string id and text")
if len({d["id"] for d in documents}) != len(documents):
    block("document ids must be unique")
if not documents:
    if False:  # seeded_empty_defect
        block("seeded edge-case defect rejected empty evidence",
              "repair source; seeded_defect=false")
    result = {"answer": "No evidence available", "citations": []}
else:
    role = "writer" if FAMILY == "reviewed_brief" else "answerer"
    result = compose(role, question, documents)
    if FAMILY == "reviewed_brief":
        for attempt in range(2):
            # Only the explicit handoff enters this NEW reviewer context, never writer messages.
            review = model("reviewer", {"question": question, "draft": result,
                           "evidence": documents, "task": "Approve only fully supported claims; "
                           "reject incorrect or irrelevant answers with feedback."})
            if review.get("action") == "approve":
                result["content_review"] = {"approved": True,
                                            "feedback": review.get("feedback", "")}
                break
            if review.get("action") != "reject" or attempt == 1:
                block("independent reviewer did not approve the brief")
            trace.append({"actor": "writer", "event": "revision", "attempt": attempt + 1})
            result = compose("writer", question, documents,
                             {"draft": result, "feedback": review.get("feedback", "")})
print(json.dumps({"accepted": True, "task_family": FAMILY, **result, "trace": trace}))
