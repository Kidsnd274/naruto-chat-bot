"""People page: everyone the bot knows, across chats. Rename, set aliases,
merge two people (one person with two accounts) or split an account off."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from naruto.db.people import Person
from naruto.services import Services
from naruto.web.auth import require_admin
from naruto.web.templating import flash

router = APIRouter(dependencies=[Depends(require_admin)])


def _services(request: Request) -> Services:
    return request.app.state.services


def _person_or_404(services: Services, person_id: int) -> Person:
    person = services.people.get(person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Unknown person (maybe merged into someone).")
    return person


def _back(person_id: int) -> RedirectResponse:
    return RedirectResponse(f"/people/{person_id}", status_code=303)


@router.get("/people")
async def people_page(request: Request):
    services = _services(request)
    query = (request.query_params.get("q") or "").strip()
    return request.app.state.templates.TemplateResponse(request, "people.html", {
        "summaries": services.people.all(query or None),
        "query": query,
    })


@router.get("/people/{person_id}")
async def person_detail(request: Request, person_id: int):
    services = _services(request)
    person = _person_or_404(services, person_id)
    titles = {chat.chat_id: chat.display_title for chat in services.chats.list_all()}
    others = [s.person for s in services.people.all() if s.person.id != person.id]
    return request.app.state.templates.TemplateResponse(request, "person_detail.html", {
        "person": person,
        "chats": [(chat_id, titles.get(chat_id, str(chat_id)), count)
                  for chat_id, count in services.people.chats_of(person.id)],
        "others": sorted(others, key=lambda p: p.display_name.lower()),
    })


@router.post("/people/{person_id}/name")
async def rename(request: Request, person_id: int):
    services = _services(request)
    person = _person_or_404(services, person_id)
    name = ((await request.form()).get("name") or "").strip()
    services.people.set_name(person.id, name or None)
    person = services.people.get(person.id)
    flash(request, f"Now shown as {person.display_name}." if name
          else f"Name cleared: shown by their Telegram name, {person.display_name}.")
    return _back(person.id)


@router.post("/people/{person_id}/aliases")
async def add_alias(request: Request, person_id: int):
    services = _services(request)
    person = _person_or_404(services, person_id)
    alias = ((await request.form()).get("alias") or "").strip()
    if alias:
        services.people.add_alias(person.id, alias)
    return _back(person.id)


@router.post("/people/{person_id}/aliases/delete")
async def remove_alias(request: Request, person_id: int):
    services = _services(request)
    person = _person_or_404(services, person_id)
    services.people.remove_alias(person.id, (await request.form()).get("alias") or "")
    return _back(person.id)


@router.post("/people/{person_id}/merge")
async def merge(request: Request, person_id: int):
    """Two steps: confirm, then merge this person into the chosen one."""
    services = _services(request)
    person = _person_or_404(services, person_id)
    form = await request.form()
    try:
        target_id = int(form.get("target") or "")
    except ValueError:
        raise HTTPException(status_code=400, detail="Choose who to merge into.") from None
    target = _person_or_404(services, target_id)
    if target.id == person.id:
        raise HTTPException(status_code=400, detail="Choose someone else.")
    if form.get("confirm") != "yes":
        accounts = ", ".join(a.label for a in person.accounts) or "no accounts"
        return request.app.state.templates.TemplateResponse(request, "confirm.html", {
            "title": f"Merge {person.display_name} into {target.display_name}?",
            "message": f"{person.display_name}'s Telegram accounts ({accounts}) and aliases move "
                       f"to {target.display_name}, and every chat will treat them as one person. "
                       "You can split an account off again later.",
            "action": f"/people/{person.id}/merge",
            "fields": {"target": target.id, "confirm": "yes"},
            "confirm_label": "Merge",
            "cancel": f"/people/{person.id}",
        })
    merged = services.people.merge(person.id, target.id)
    flash(request, f"Merged {person.display_name} into {merged.display_name}.")
    return _back(merged.id)


@router.post("/people/{person_id}/split")
async def split(request: Request, person_id: int):
    services = _services(request)
    person = _person_or_404(services, person_id)
    try:
        user_id = int((await request.form()).get("user_id") or "")
    except ValueError:
        raise HTTPException(status_code=400, detail="Choose an account.") from None
    if user_id not in {a.user_id for a in person.accounts}:
        raise HTTPException(status_code=400, detail="That account isn't this person's.")
    try:
        new = services.people.split(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    flash(request, f"{new.display_name} is now a separate person.")
    return _back(new.id)
