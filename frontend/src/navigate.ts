// Leaving the app for another site (Google's consent screen). Its own module so
// tests can replace it: jsdom cannot navigate.
export function goTo(url: string): void {
  window.location.assign(url);
}
