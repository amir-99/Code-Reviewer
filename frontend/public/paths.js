// Resolve beside the module so root and subpath deployments use the same build.
export function apiURL(path, moduleURL = import.meta.url) {
  return new URL('./api' + path, moduleURL);
}
