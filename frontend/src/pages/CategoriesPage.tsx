// Categories (PRD §5.3): a short list, renamed and extended freely, archived
// rather than deleted. Archiving makes a category's transactions uncategorized
// until it is restored; nothing about them is changed in between.
import { type FormEvent, useState } from "react";

import {
  type Category,
  useArchiveCategory,
  useCategories,
  useCreateCategory,
  useRenameCategory,
  useRestoreCategory,
} from "../api/categories";
import { describeError } from "../api/client";
import "../styles/categories.css";

export function CategoriesPage() {
  const categories = useCategories();
  const create = useCreateCategory();
  const [name, setName] = useState("");
  const [showArchived, setShowArchived] = useState(false);

  const all = categories.data ?? [];
  const active = all.filter((c) => c.archived_at === null);
  const archived = all.filter((c) => c.archived_at !== null);

  const add = (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim()) return;
    create.mutate(name.trim(), { onSuccess: () => setName("") });
  };

  return (
    <>
      <header className="page-head"><h1 className="page-title">Categories</h1></header>
      <p className="muted category-intro">
        Keep the list short: the detail lives in the payees under each category.
      </p>

      {categories.isPending && <p className="muted">Loading…</p>}
      {categories.isError && <p className="form-error" role="alert">{describeError(categories.error)}</p>}

      <ul className="category-list">
        {active.map((c) => <CategoryItem key={c.id} category={c} />)}
      </ul>

      <form className="category-add" onSubmit={add}>
        <label className="field__label" htmlFor="new-category">New category</label>
        <div className="category-add__row">
          <input id="new-category" className="field__input" value={name} onChange={(e) => setName(e.target.value)}
            placeholder="Pets" />
          <button className="button button--primary" type="submit" disabled={create.isPending}>Add category</button>
        </div>
        {create.isError && <p className="form-error" role="alert">{describeError(create.error)}</p>}
      </form>

      {archived.length > 0 && (
        <section className="archived">
          <button className="button button--quiet" type="button" aria-expanded={showArchived}
            onClick={() => setShowArchived((v) => !v)}>
            {showArchived ? "Hide archived" : `Show archived (${archived.length})`}
          </button>
          {showArchived && (
            <ul className="category-list category-list--archived">
              {archived.map((c) => <CategoryItem key={c.id} category={c} />)}
            </ul>
          )}
        </section>
      )}
    </>
  );
}

function CategoryItem({ category }: { category: Category }) {
  const rename = useRenameCategory();
  const archive = useArchiveCategory();
  const restore = useRestoreCategory();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(category.name);
  const isArchived = category.archived_at !== null;
  const failed = rename.error ?? archive.error ?? restore.error;

  const save = (event: FormEvent) => {
    event.preventDefault();
    rename.mutate({ id: category.id, name }, { onSuccess: () => setEditing(false) });
  };

  return (
    <li className="category" aria-label={category.name}>
      {editing ? (
        <form className="category__rename" onSubmit={save}>
          <input className="field__input" aria-label={`Name for ${category.name}`} value={name} autoFocus
            onChange={(e) => setName(e.target.value)} />
          <button className="button button--primary" type="submit" disabled={rename.isPending}>Save</button>
          <button className="button button--quiet" type="button" onClick={() => setEditing(false)}>Cancel</button>
        </form>
      ) : (
        <>
          <span className="category__name">{category.name}</span>
          <div className="actions">
            {isArchived ? (
              <button className="button button--quiet" type="button" onClick={() => restore.mutate(category.id)}>
                Restore
              </button>
            ) : (
              <>
                <button className="button button--quiet" type="button" onClick={() => setEditing(true)}>Rename</button>
                <button className="button button--quiet" type="button" onClick={() => archive.mutate(category.id)}>
                  Archive
                </button>
              </>
            )}
          </div>
        </>
      )}
      {failed && <p className="form-error" role="alert">{describeError(failed)}</p>}
    </li>
  );
}
