import dataclasses

import pytest

from prompt_studio.auth import Accounts, decode_hash, hash_password, sign_out
from prompt_studio.errors import GenerationError
from prompt_studio.models import CharacterFields


def test_salted_hash_and_authentication(accounts, admin):
    first, second = hash_password("a-secure-password"), hash_password("a-secure-password")
    assert first != second
    assert "a-secure-password" not in first
    assert accounts.authenticate("admin", "wrong") is None
    assert accounts.authenticate("unknown", "admin-test-password") is None
    assert admin.role == "admin"


def test_disabled_and_stale_sessions(accounts, admin):
    accounts.users["admin"]["enabled"] = False
    assert accounts.authenticate("admin", "admin-test-password") is None
    with pytest.raises(GenerationError):
        accounts.require(admin)
    accounts.users["admin"]["enabled"] = True
    accounts.users["admin"]["password_hash"] = hash_password("changed-password")
    with pytest.raises(GenerationError):
        accounts.require(admin)


def test_roles_cannot_be_forged(accounts, creator):
    with pytest.raises(GenerationError):
        accounts.require(dataclasses.replace(creator, role="admin"), admin=True)


def test_sign_out_removes_session_state(admin):
    session = {"principal": admin, "last_job_id": "example", "preview": "example"}
    sign_out(session)
    assert session == {}


@pytest.mark.parametrize(
    "encoded", ["plaintext", "scrypt$999999999$8$1$AAAA$AAAA", "scrypt$16384$8$1$AAAA$AAAA"]
)
def test_bad_hashes_rejected(encoded):
    with pytest.raises(ValueError):
        decode_hash(encoded)


def test_requires_exactly_two_roles(accounts):
    config = dict(accounts.users)
    config["creator"] = {**config["creator"], "role": "admin"}
    with pytest.raises(GenerationError):
        Accounts(config)


def test_status_does_not_expose_hashes(accounts, admin, creator):
    status = accounts.status(admin)
    assert all("password_hash" not in row for row in status)
    with pytest.raises(GenerationError):
        accounts.status(creator)


@pytest.mark.parametrize("method", ["add", "update", "archive", "delete"])
def test_creator_cannot_mutate(method, library, creator, character, fields, png):
    with pytest.raises(GenerationError, match="Only the admin"):
        if method == "add":
            library.add(creator, fields, png)
        elif method == "update":
            library.update(creator, character.id, character.revision, fields)
        elif method == "archive":
            library.set_status(creator, character.id, character.revision, "archived")
        else:
            library.delete(creator, character.id, character.revision, character.slug)


def test_casefold_duplicate_and_slug_collision(library, admin, character, fields, png):
    with pytest.raises(ValueError, match="name"):
        library.add(admin, fields.model_copy(update={"display_name": "mAyA"}), png)
    with pytest.raises(ValueError, match="slug"):
        library.add(admin, fields.model_copy(update={"display_name": "Another"}), png, slug="MAYA!")


def test_blank_description_or_invalid_mapping_rejected(fields):
    for changes in [
        {"wardrobe": "  "},
        {"mapping": "@image_2 = Another person"},
        {"mapping": "@image_1 = Person\nsecond line"},
    ]:
        with pytest.raises(ValueError):
            CharacterFields(**{**fields.model_dump(), **changes})


def test_image_validation(library, admin, fields):
    with pytest.raises(ValueError, match="reference image"):
        library.add(admin, fields, b"bad image")


def test_version_and_stale_edit(library, admin, character, fields):
    updated = library.update(
        admin,
        character.id,
        character.revision,
        fields.model_copy(update={"display_name": "New Maya"}),
    )
    assert updated.id == character.id and updated.slug == character.slug
    assert updated.version == 2
    with pytest.raises(GenerationError, match="another session"):
        library.update(admin, character.id, character.revision, fields)


def test_archive_restore_defaults_and_versions(library, admin, creator, character):
    archived = library.set_status(admin, character.id, character.revision, "archived")
    assert not library.list(creator)
    assert len(library.list(creator, include_archived=True)) == 1
    restored = library.set_status(admin, character.id, archived.revision, "active")
    assert restored.version == character.version
    assert library.list(creator)[0].status == "active"


def test_confirmed_deletion(library, admin, character):
    with pytest.raises(ValueError, match="confirm"):
        library.delete(admin, character.id, character.revision, "yes")
    library.delete(admin, character.id, character.revision, character.slug)
    assert not library.list(admin, include_archived=True)


def test_image_replacement_increments_version_without_changing_identity(
    library, admin, creator, character, fields
):
    import io

    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (20, 20), "blue").save(output, "PNG")
    updated = library.update(admin, character.id, character.revision, fields, output.getvalue())
    assert updated.version == 2
    assert updated.id == character.id and updated.slug == character.slug
    assert updated.image_id != character.image_id
    assert library.image(creator, updated)


def test_interrupted_image_edit_keeps_current_document_consistent(
    library, admin, creator, character, fields, drive
):
    import io

    from conftest import Interruption
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (20, 20), "blue").save(output, "PNG")
    old_image = library.image(creator, character)

    def crash(position, name, file_id, data):
        if position == "before" and name == "character.json":
            raise Interruption()

    drive.hook = crash
    with pytest.raises(Interruption):
        library.update(admin, character.id, character.revision, fields, output.getvalue())
    drive.hook = None
    unchanged = library.get(creator, character.id)
    assert unchanged.image_id == character.image_id
    assert library.image(creator, unchanged) == old_image
