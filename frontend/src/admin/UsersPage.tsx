// User management (PRD §6.9): the admin creates people (no self-signup), resets passwords, changes roles. A
// reset signs the person out everywhere; the last admin can't be made a member (the server says so).
import { type FormEvent, useState } from "react";

import { describeError } from "../api/client";
import { type ManagedUser, useAddUser, useChangeRole, useResetPassword, useUsers } from "../api/users";

export function UsersPage() {
  const users = useUsers();
  const [adding, setAdding] = useState(false);
  return (
    <div className="admin-users">
      <header className="page-head">
        <h1 className="page-title">Users</h1>
        {!adding && <button className="button button--quiet" type="button" onClick={() => setAdding(true)}>Add a person</button>}
      </header>
      <p className="admin__lead">
        Each person signs in with their own password, connects their own Gmail and sets up their own accounts.
        Admins also see these screens.
      </p>
      {adding && <AddForm onDone={() => setAdding(false)} />}
      {users.isError && <p className="form-error" role="alert">{describeError(users.error)}</p>}
      {users.data && (
        <ul className="admin-users__list" aria-label="People">
          {users.data.map((u) => <Person key={u.id} u={u} />)}
        </ul>
      )}
    </div>
  );
}

function AddForm({ onDone }: { onDone: () => void }) {
  const add = useAddUser();
  const [name, setName] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("member");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    add.mutate({ display_name: name, username, password, role }, { onSuccess: onDone });
  };
  return (
    <form className="admin-users__add sheet" onSubmit={submit} aria-label="Add a person">
      <label className="field"><span className="field__label">Name</span>
        <input className="field__input" value={name} onChange={(e) => setName(e.target.value)} required /></label>
      <label className="field"><span className="field__label">Username</span>
        <input className="field__input" value={username} onChange={(e) => setUsername(e.target.value)} required
          autoCapitalize="none" autoComplete="off" /></label>
      <label className="field"><span className="field__label">Password</span>
        <input className="field__input" type="password" value={password} onChange={(e) => setPassword(e.target.value)}
          required minLength={8} autoComplete="new-password" /></label>
      <label className="field"><span className="field__label">Role</span>
        <select className="field__input" value={role} onChange={(e) => setRole(e.target.value)}>
          <option value="member">Member</option>
          <option value="admin">Admin</option>
        </select></label>
      {add.isError && <p className="form-error" role="alert">{describeError(add.error)}</p>}
      <div className="actions">
        <button className="button button--quiet" type="button" onClick={onDone}>Cancel</button>
        <button className="button button--primary" type="submit" disabled={add.isPending}>Add</button>
      </div>
    </form>
  );
}

function Person({ u }: { u: ManagedUser }) {
  const reset = useResetPassword();
  const role = useChangeRole();
  const [resetting, setResetting] = useState(false);
  const [password, setPassword] = useState("");
  const [done, setDone] = useState<string | null>(null);
  const other = u.role === "admin" ? "member" : "admin";
  const submit = (e: FormEvent) => {
    e.preventDefault();
    reset.mutate({ id: u.id, password }, { onSuccess: () => {
      setResetting(false); setPassword(""); setDone(`Password changed. ${u.display_name} is signed out everywhere.`);
    } });
  };
  return (
    <li className="admin-users__person" aria-label={u.display_name}>
      <span className="admin-users__who">
        <span className="admin-users__name">{u.display_name}</span>
        <span className="muted">{u.username}, {u.role}</span>
      </span>
      <span className="actions admin-users__actions">
        <button className="button button--quiet" type="button" onClick={() => { setResetting(!resetting); setDone(null); }}>
          Reset password
        </button>
        <button className="button button--quiet" type="button" disabled={role.isPending}
          onClick={() => role.mutate({ id: u.id, role: other })}>Make {other}</button>
      </span>
      {resetting && (
        <form className="admin-users__reset" onSubmit={submit}>
          <label className="field"><span className="field__label">New password for {u.display_name}</span>
            <input className="field__input" type="password" value={password} minLength={8} required
              autoComplete="new-password" onChange={(e) => setPassword(e.target.value)} /></label>
          <button className="button button--primary" type="submit" disabled={reset.isPending}>Set password</button>
        </form>
      )}
      {done && <p className="admin-users__done" role="status">{done}</p>}
      {(reset.isError || role.isError) && (
        <p className="form-error" role="alert">{describeError(reset.error ?? role.error)}</p>
      )}
    </li>
  );
}
