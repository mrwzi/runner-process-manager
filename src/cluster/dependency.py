from __future__ import annotations


class DependencyError(ValueError):
    pass


def startup_order(apps: list[dict], selected_ids: set[str] | None = None) -> list[str]:
    by_id = {str(app["id"]): app for app in apps}
    selected = set(by_id) if selected_ids is None else set(selected_ids)

    def add_dependencies(app_id: str) -> None:
        if app_id not in by_id:
            raise DependencyError(f"Unknown application dependency: {app_id}")
        for dependency in by_id[app_id].get("dependencies", []):
            dependency = str(dependency)
            if dependency not in selected:
                selected.add(dependency)
                add_dependencies(dependency)

    for app_id in tuple(selected):
        add_dependencies(app_id)

    visiting: set[str] = set()
    visited: set[str] = set()
    result: list[str] = []

    def visit(app_id: str, chain: list[str]) -> None:
        if app_id in visiting:
            raise DependencyError("Dependency cycle: " + " -> ".join([*chain, app_id]))
        if app_id in visited:
            return
        visiting.add(app_id)
        for dependency in by_id[app_id].get("dependencies", []):
            visit(str(dependency), [*chain, app_id])
        visiting.remove(app_id)
        visited.add(app_id)
        result.append(app_id)

    for app_id in by_id:
        if app_id in selected:
            visit(app_id, [])
    return result


def shutdown_order(apps: list[dict], selected_ids: set[str] | None = None) -> list[str]:
    return list(reversed(startup_order(apps, selected_ids)))
