"""Read projection of original history against live Runtime control ownership."""


def project_interaction_state(snapshot, records):
    live = [item for item in snapshot.executions
            if not item.state.terminal and item.generation == snapshot.generation]
    controls = {}
    for item in live:
        for control_id in (*item.waiting_control_ids, item.waiting_control_id):
            if control_id:
                controls[control_id] = item.created_at
    questions = {}
    for record in records or ():
        control_id = record.get("request_id")
        if (record.get("event_type") != "chat.ask_user_question"
                or control_id not in controls
                or record.get("timestamp", 0) < controls[control_id]):
            continue
        questions[control_id] = {
            key: record[key] for key in (
                "request_id", "source", "questions", "approval_schema",
                "evolution_meta", "plan_approval_kind", "plan_content", "plan_language",
                "swarmflow_meta",
            ) if key in record
        }
        questions[control_id]["session_generation"] = snapshot.generation
    return {"is_processing": bool(live), "pending_interactions": list(questions.values())}
