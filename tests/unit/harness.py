from ops.testing import Harness


class TmpHarness(Harness):
    """Temporary Harness object.
    NOTE (rgildein): This object should be removed after a successful merge [PR460].
    [PR460]: https://github.com/canonical/operator/pull/460
    """
    def remove_relation(self, relation_name: str, remote_app: str) -> None:
        """Remove a relation."""
        rel_id = self._backend._relation_ids_map[relation_name][0]
        for unit_name in self._backend._relation_list_map[rel_id]:
            self.remove_relation_unit(rel_id, unit_name)
        self._emit_relation_broken(relation_name, rel_id, remote_app)
        self._backend._relation_app_and_units.pop(rel_id)
        self._backend._relation_data.pop(rel_id)
        self._backend._relation_list_map.pop(rel_id)
        self._backend._relation_ids_map.pop(relation_name)
        self._backend._relation_names.pop(rel_id)

    def remove_relation_unit(self, relation_id: int, remote_unit_name: str) -> None:
        """Remove a unit from a relation."""
        relation_name = self._backend._relation_names[relation_id]

        # gather data to invalidate cache later
        remote_unit = self._model.get_unit(remote_unit_name)
        relation = self._model.get_relation(relation_name, relation_id)
        unit_cache = relation.data.get(remote_unit, None)

        # statements which could access cache
        self._emit_relation_departed(relation_id, remote_unit_name)
        self._backend._relation_data[relation_id].pop(remote_unit_name)
        self._backend._relation_app_and_units[relation_id]["units"].remove(remote_unit_name)
        self._backend._relation_list_map[relation_id].remove(remote_unit_name)

        if unit_cache is not None:
            unit_cache._invalidate()

    def _emit_relation_departed(self, relation_id, unit_name):
        """Trigger relation-departed event for a given relation id and unit."""
        if self._charm is None or not self._hooks_enabled:
            return
        rel_name = self._backend._relation_names[relation_id]
        relation = self.model.get_relation(rel_name, relation_id)
        if '/' in unit_name:
            app_name = unit_name.split('/')[0]
            app = self.model.get_app(app_name)
            unit = self.model.get_unit(unit_name)
        else:
            raise ValueError('Invalid Unit Name')
        self._charm.on[rel_name].relation_departed.emit(relation, app, unit)

    def _emit_relation_broken(self, relation_name: str, relation_id: int, remote_app: str) -> None:
        """Trigger relation-broken for a given relation with a given remote application."""
        if self._charm is None or not self._hooks_enabled:
            return
        relation = self._model.get_relation(relation_name, relation_id)
        app = self._model.get_app(remote_app)
        self._charm.on[relation_name].relation_broken.emit(relation, app)
