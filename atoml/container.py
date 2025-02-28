import copy

from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

from ._compat import decode
from ._utils import merge_dicts
from .exceptions import ATOMLError, KeyAlreadyPresent, NonExistentKey
from .items import AoT, Comment, Item, Key, Null, Table, Whitespace, _CustomDict
from .items import item as _item


_NOT_SET = object()


class Container(_CustomDict):
    """
    A container for items within a TOMLDocument.
    """

    def __init__(self, parsed: bool = False) -> None:
        self._map: Dict[Key, int] = {}
        self._body: List[Tuple[Optional[Key], Item]] = []
        self._parsed = parsed
        self._table_keys = []

    @property
    def body(self) -> List[Tuple[Optional[Key], Item]]:
        return self._body

    @property
    def value(self) -> Dict[Any, Any]:
        d = {}
        for k, v in self._body:
            if k is None:
                continue

            k = k.key
            v = v.value

            if isinstance(v, Container):
                v = v.value

            if k in d:
                merge_dicts(d[k], v)
            else:
                d[k] = v

        return d

    def parsing(self, parsing: bool) -> None:
        self._parsed = parsing

        for _, v in self._body:
            if isinstance(v, Table):
                v.value.parsing(parsing)
            elif isinstance(v, AoT):
                for t in v.body:
                    t.value.parsing(parsing)

    def add(
        self, key: Union[Key, Item, str], item: Optional[Item] = None
    ) -> "Container":
        """
        Adds an item to the current Container.
        """
        if item is None:
            if not isinstance(key, (Comment, Whitespace)):
                raise ValueError(
                    "Non comment/whitespace items must have an associated key"
                )

            key, item = None, key

        return self.append(key, item)

    def append(self, key: Union[Key, str, None], item: Item) -> "Container":
        if not isinstance(key, Key) and key is not None:
            key = Key(key)

        if not isinstance(item, Item):
            item = _item(item)

        if isinstance(item, (AoT, Table)) and item.name is None:
            item.name = key.key

        prev = self._previous_item()
        prev_ws = isinstance(prev, Whitespace) or ends_with_withespace(prev)
        if isinstance(item, Table):
            if item.name != key.key:
                item.invalidate_display_name()
            if self._body and not (self._parsed or item.trivia.indent or prev_ws):
                item.trivia.indent = "\n"

        if isinstance(item, AoT) and self._body and not self._parsed:
            item.invalidate_display_name()
            if item and not ("\n" in item[0].trivia.indent or prev_ws):
                item[0].trivia.indent = "\n" + item[0].trivia.indent

        if key is not None and key in self:
            current_idx = self._map[key]
            if isinstance(current_idx, tuple):
                current_body_element = self._body[current_idx[-1]]
            else:
                current_body_element = self._body[current_idx]

            current = current_body_element[1]

            if isinstance(item, Table):
                if not isinstance(current, (Table, AoT)):
                    raise KeyAlreadyPresent(key)

                if item.is_aot_element():
                    # New AoT element found later on
                    # Adding it to the current AoT
                    if not isinstance(current, AoT):
                        current = AoT([current, item], parsed=self._parsed)

                        self._replace(key, key, current)
                    else:
                        current.append(item)

                    return self
                elif current.is_aot():
                    if not item.is_aot_element():
                        # Tried to define a table after an AoT with the same name.
                        raise KeyAlreadyPresent(key)

                    current.append(item)

                    return self
                elif current.is_super_table():
                    if item.is_super_table():
                        # We need to merge both super tables
                        if (
                            self._table_keys[-1] != current_body_element[0]
                            or key.is_dotted()
                            or current_body_element[0].is_dotted()
                        ):
                            if not isinstance(current_idx, tuple):
                                current_idx = (current_idx,)

                            self._map[key] = current_idx + (len(self._body),)
                            self._body.append((key, item))
                            self._table_keys.append(key)

                            # Building a temporary proxy to check for errors
                            OutOfOrderTableProxy(self, self._map[key])

                            return self

                        for k, v in item.value.body:
                            current.append(k, v)

                        return self
                    elif current_body_element[0].is_dotted():
                        raise ATOMLError("Redefinition of an existing table")
                elif not item.is_super_table():
                    raise KeyAlreadyPresent(key)
            elif isinstance(item, AoT):
                if not isinstance(current, AoT):
                    # Tried to define an AoT after a table with the same name.
                    raise KeyAlreadyPresent(key)

                for table in item.body:
                    current.append(table)

                return self
            else:
                raise KeyAlreadyPresent(key)

        is_table = isinstance(item, (Table, AoT))
        if key is not None and self._body and not self._parsed:
            # If there is already at least one table in the current container
            # and the given item is not a table, we need to find the last
            # item that is not a table and insert after it
            # If no such item exists, insert at the top of the table
            key_after = None
            for i, (k, v) in enumerate(self._body):
                if isinstance(v, Null):
                    continue  # Null elements are inserted after deletion

                if isinstance(v, Whitespace) and not v.is_fixed():
                    continue

                if not is_table and isinstance(v, (Table, AoT)):
                    break

                key_after = k or i  # last scalar, Array or InlineTable value

            if key_after is not None:
                if isinstance(key_after, int):
                    if key_after + 1 < len(self._body):
                        return self._insert_at(key_after + 1, key, item)
                    else:
                        previous_item = self._body[-1][1]
                        if not (
                            isinstance(previous_item, Whitespace)
                            or ends_with_withespace(previous_item)
                            or is_table
                            or "\n" in previous_item.trivia.trail
                        ):
                            previous_item.trivia.trail += "\n"
                else:
                    return self._insert_after(key_after, key, item)
            else:
                return self._insert_at(0, key, item)

        if key in self._map:
            current_idx = self._map[key]
            if isinstance(current_idx, tuple):
                current_idx = current_idx[-1]

            current = self._body[current_idx][1]
            if key is not None and not isinstance(current, Table):
                raise KeyAlreadyPresent(key)

            # Adding sub tables to a currently existing table
            if not isinstance(current_idx, tuple):
                current_idx = (current_idx,)

            self._map[key] = current_idx + (len(self._body),)
        else:
            self._map[key] = len(self._body)

        self._body.append((key, item))
        if item.is_table():
            self._table_keys.append(key)

        if key is not None:
            dict.__setitem__(self, key.key, item.value)

        return self

    def remove(self, key: Union[Key, str]) -> "Container":
        if not isinstance(key, Key):
            key = Key(key)

        idx = self._map.pop(key, None)
        if idx is None:
            raise NonExistentKey(key)

        if isinstance(idx, tuple):
            for i in idx:
                self._body[i] = (None, Null())
        else:
            self._body[idx] = (None, Null())

        dict.__delitem__(self, key.key)

        return self

    def _insert_after(
        self, key: Union[Key, str], other_key: Union[Key, str], item: Any
    ) -> "Container":
        if key is None:
            raise ValueError("Key cannot be null in insert_after()")

        if key not in self:
            raise NonExistentKey(key)

        if not isinstance(key, Key):
            key = Key(key)

        if not isinstance(other_key, Key):
            other_key = Key(other_key)

        item = _item(item)

        idx = self._map[key]
        # Insert after the max index if there are many.
        if isinstance(idx, tuple):
            idx = max(idx)
        current_item = self._body[idx][1]
        if "\n" not in current_item.trivia.trail:
            current_item.trivia.trail += "\n"

        # Increment indices after the current index
        for k, v in self._map.items():
            if isinstance(v, tuple):
                new_indices = []
                for v_ in v:
                    if v_ > idx:
                        v_ = v_ + 1

                    new_indices.append(v_)

                self._map[k] = tuple(new_indices)
            elif v > idx:
                self._map[k] = v + 1

        self._map[other_key] = idx + 1
        self._body.insert(idx + 1, (other_key, item))

        if key is not None:
            dict.__setitem__(self, other_key.key, item.value)

        return self

    def _insert_at(self, idx: int, key: Union[Key, str], item: Any) -> "Container":
        if idx > len(self._body) - 1:
            raise ValueError(f"Unable to insert at position {idx}")

        if not isinstance(key, Key):
            key = Key(key)

        item = _item(item)

        if idx > 0:
            previous_item = self._body[idx - 1][1]
            if not (
                isinstance(previous_item, Whitespace)
                or ends_with_withespace(previous_item)
                or isinstance(item, (AoT, Table))
                or "\n" in previous_item.trivia.trail
            ):
                previous_item.trivia.trail += "\n"

        # Increment indices after the current index
        for k, v in self._map.items():
            if isinstance(v, tuple):
                new_indices = []
                for v_ in v:
                    if v_ >= idx:
                        v_ = v_ + 1

                    new_indices.append(v_)

                self._map[k] = tuple(new_indices)
            elif v >= idx:
                self._map[k] = v + 1

        self._map[key] = idx
        self._body.insert(idx, (key, item))

        if key is not None:
            dict.__setitem__(self, key.key, item.value)

        return self

    def item(self, key: Union[Key, str]) -> Item:
        if not isinstance(key, Key):
            key = Key(key)

        idx = self._map.get(key, None)
        if idx is None:
            raise NonExistentKey(key)

        if isinstance(idx, tuple):
            # The item we are getting is an out of order table
            # so we need a proxy to retrieve the proper objects
            # from the parent container
            return OutOfOrderTableProxy(self, idx)

        return self._body[idx][1]

    def last_item(self) -> Optional[Item]:
        if self._body:
            return self._body[-1][1]

    def as_string(self) -> str:
        s = ""
        for k, v in self._body:
            if k is not None:
                if isinstance(v, Table):
                    s += self._render_table(k, v)
                elif isinstance(v, AoT):
                    s += self._render_aot(k, v)
                else:
                    s += self._render_simple_item(k, v)
            else:
                s += self._render_simple_item(k, v)

        return s

    def _render_table(
        self, key: Key, table: Table, prefix: Optional[str] = None
    ) -> str:
        cur = ""

        if table.display_name is not None:
            _key = table.display_name
        else:
            _key = key.as_string()

            if prefix is not None:
                _key = prefix + "." + _key

        if not table.is_super_table() or (
            any(
                not isinstance(v, (Table, AoT, Whitespace)) for _, v in table.value.body
            )
            and not key.is_dotted()
        ):
            open_, close = "[", "]"
            if table.is_aot_element():
                open_, close = "[[", "]]"

            cur += "{}{}{}{}{}{}{}{}".format(
                table.trivia.indent,
                open_,
                decode(_key),
                close,
                table.trivia.comment_ws,
                decode(table.trivia.comment),
                table.trivia.trail,
                "\n" if "\n" not in table.trivia.trail and len(table.value) > 0 else "",
            )

        for k, v in table.value.body:
            if isinstance(v, Table):
                if v.is_super_table():
                    if k.is_dotted() and not key.is_dotted():
                        # Dotted key inside table
                        cur += self._render_table(k, v)
                    else:
                        cur += self._render_table(k, v, prefix=_key)
                else:
                    cur += self._render_table(k, v, prefix=_key)
            elif isinstance(v, AoT):
                cur += self._render_aot(k, v, prefix=_key)
            else:
                cur += self._render_simple_item(
                    k, v, prefix=_key if key.is_dotted() else None
                )

        return cur

    def _render_aot(self, key, aot, prefix=None):
        _key = key.as_string()
        if prefix is not None:
            _key = prefix + "." + _key

        cur = ""
        _key = decode(_key)
        for table in aot.body:
            cur += self._render_aot_table(table, prefix=_key)

        return cur

    def _render_aot_table(self, table: Table, prefix: Optional[str] = None) -> str:
        cur = ""

        _key = prefix or ""

        if not table.is_super_table():
            open_, close = "[[", "]]"

            cur += "{}{}{}{}{}{}{}".format(
                table.trivia.indent,
                open_,
                decode(_key),
                close,
                table.trivia.comment_ws,
                decode(table.trivia.comment),
                table.trivia.trail,
            )

        for k, v in table.value.body:
            if isinstance(v, Table):
                if v.is_super_table():
                    if k.is_dotted():
                        # Dotted key inside table
                        cur += self._render_table(k, v)
                    else:
                        cur += self._render_table(k, v, prefix=_key)
                else:
                    cur += self._render_table(k, v, prefix=_key)
            elif isinstance(v, AoT):
                cur += self._render_aot(k, v, prefix=_key)
            else:
                cur += self._render_simple_item(k, v)

        return cur

    def _render_simple_item(self, key, item, prefix=None):
        if key is None:
            return item.as_string()

        _key = key.as_string()
        if prefix is not None:
            _key = prefix + "." + _key

        return "{}{}{}{}{}{}{}".format(
            item.trivia.indent,
            decode(_key),
            key.sep,
            decode(item.as_string()),
            item.trivia.comment_ws,
            decode(item.trivia.comment),
            item.trivia.trail,
        )

    def __len__(self) -> int:
        return dict.__len__(self)

    def __iter__(self) -> Iterator[str]:
        return iter(dict.keys(self))

    # Dictionary methods
    def __getitem__(self, key: Union[Key, str]) -> Union[Item, "Container"]:
        if not isinstance(key, Key):
            key = Key(key)

        idx = self._map.get(key, None)
        if idx is None:
            raise NonExistentKey(key)

        if isinstance(idx, tuple):
            # The item we are getting is an out of order table
            # so we need a proxy to retrieve the proper objects
            # from the parent container
            return OutOfOrderTableProxy(self, idx)

        item = self._body[idx][1]
        if item.is_boolean():
            return item.value

        return item

    def __setitem__(self, key: Union[Key, str], value: Any) -> None:
        if key is not None and key in self:
            self._replace(key, key, value)
        else:
            self.append(key, value)

    def __delitem__(self, key: Union[Key, str]) -> None:
        self.remove(key)

    def setdefault(self, key: Union[Key, str], default: Any) -> Any:
        super().setdefault(key, default=default)
        return self[key]

    def _replace(
        self, key: Union[Key, str], new_key: Union[Key, str], value: Item
    ) -> None:
        if not isinstance(key, Key):
            key = Key(key)

        if not isinstance(new_key, Key):
            new_key = Key(new_key)

        idx = self._map.get(key, None)
        if idx is None:
            raise NonExistentKey(key)

        self._replace_at(idx, new_key, value)

    def _replace_at(
        self, idx: Union[int, Tuple[int]], new_key: Union[Key, str], value: Item
    ) -> None:
        if not isinstance(new_key, Key):
            new_key = Key(new_key)

        if isinstance(idx, tuple):
            for i in idx[1:]:
                self._body[i] = (None, Null())

            idx = idx[0]

        k, v = self._body[idx]

        self._map[new_key] = self._map.pop(k)
        if new_key != k:
            dict.__delitem__(self, k)

        if isinstance(self._map[new_key], tuple):
            self._map[new_key] = self._map[new_key][0]

        value = _item(value)

        if isinstance(value, (AoT, Table)) and not isinstance(v, (AoT, Table)):
            # new tables should appear after all non-table values
            self.remove(k)
            for i in range(idx, len(self._body)):
                if isinstance(self._body[i][1], (AoT, Table)):
                    self._insert_at(i, new_key, value)
                    idx = i
                    break
            else:
                idx = -1
                self.append(new_key, value)
        else:
            # Copying trivia
            if not isinstance(value, (Whitespace, AoT)):
                value.trivia.indent = v.trivia.indent
                value.trivia.comment_ws = value.trivia.comment_ws or v.trivia.comment_ws
                value.trivia.comment = value.trivia.comment or v.trivia.comment
                value.trivia.trail = v.trivia.trail
            self._body[idx] = (new_key, value)

        if hasattr(value, "invalidate_display_name"):
            value.invalidate_display_name()  # type: ignore[attr-defined]

        if isinstance(value, Table):
            # Insert a cosmetic new line for tables if:
            # - it does not have it yet OR is not followed by one
            # - it is not the last item
            last, _ = self._previous_item_with_index()
            idx = last if idx < 0 else idx
            has_ws = ends_with_withespace(value)
            next_ws = idx < last and isinstance(self._body[idx + 1][1], Whitespace)
            if idx < last and not (next_ws or has_ws):
                value.append(None, Whitespace("\n"))

            dict.__setitem__(self, new_key.key, value.value)

    def __str__(self) -> str:
        return str(self.value)

    def __repr__(self) -> str:
        return repr(self.value)

    def __eq__(self, other: dict) -> bool:
        if not isinstance(other, dict):
            return NotImplemented

        return self.value == other

    def _getstate(self, protocol):
        return (self._parsed,)

    def __reduce__(self):
        return self.__reduce_ex__(2)

    def __reduce_ex__(self, protocol):
        return (
            self.__class__,
            self._getstate(protocol),
            (self._map, self._body, self._parsed, self._table_keys),
        )

    def __setstate__(self, state):
        self._map = state[0]
        self._body = state[1]
        self._parsed = state[2]
        self._table_keys = state[3]

        for key, item in self._body:
            if key is not None:
                dict.__setitem__(self, key.key, item.value)

    def copy(self) -> "Container":
        return copy.copy(self)

    def __copy__(self) -> "Container":
        c = self.__class__(self._parsed)
        for k, v in dict.items(self):
            dict.__setitem__(c, k, v)

        c._body += self.body
        c._map.update(self._map)

        return c

    def _previous_item_with_index(
        self, idx: Optional[int] = None, ignore=(Null,)
    ) -> Optional[Tuple[int, Item]]:
        """Find the immediate previous item before index ``idx``"""
        if idx is None or idx > len(self._body):
            idx = len(self._body)
        for i in range(idx - 1, -1, -1):
            v = self._body[i][-1]
            if not isinstance(v, ignore):
                return i, v
        return None

    def _previous_item(
        self, idx: Optional[int] = None, ignore=(Null,)
    ) -> Optional[Item]:
        """Find the immediate previous item before index ``idx``.
        If ``idx`` is not given, the last item is returned.
        """
        prev = self._previous_item_with_index(idx, ignore)
        return prev[-1] if prev else None


class OutOfOrderTableProxy(_CustomDict):
    def __init__(self, container: Container, indices: Tuple[int]) -> None:
        self._container = container
        self._internal_container = Container(True)
        self._tables = []
        self._tables_map = {}
        self._map = {}

        for i in indices:
            key, item = self._container._body[i]

            if isinstance(item, Table):
                self._tables.append(item)
                table_idx = len(self._tables) - 1
                for k, v in item.value.body:
                    self._internal_container.append(k, v)
                    self._tables_map[k] = table_idx
                    if k is not None:
                        dict.__setitem__(self, k.key, v)
            else:
                self._internal_container.append(key, item)
                self._map[key] = i
                if key is not None:
                    dict.__setitem__(self, key.key, item)

    @property
    def value(self):
        return self._internal_container.value

    def __getitem__(self, key: Union[Key, str]) -> Any:
        if key not in self._internal_container:
            raise NonExistentKey(key)

        return self._internal_container[key]

    def __setitem__(self, key: Union[Key, str], item: Any) -> None:
        if key in self._map:
            idx = self._map[key]
            self._container._replace_at(idx, key, item)
        elif key in self._tables_map:
            table = self._tables[self._tables_map[key]]
            table[key] = item
        elif self._tables:
            table = self._tables[0]
            table[key] = item
        else:
            self._container[key] = item

        self._internal_container[key] = item
        if key is not None:
            dict.__setitem__(self, key, item)

    def __delitem__(self, key: Union[Key, str]) -> None:
        if key in self._map:
            del self._container[key]
            del self._map[key]
        elif key in self._tables_map:
            table = self._tables[self._tables_map[key]]
            del table[key]
            del self._tables_map[key]
        else:
            raise NonExistentKey(key)

        del self._internal_container[key]
        if key is not None:
            dict.__delitem__(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(dict.keys(self))

    def __len__(self) -> int:
        return dict.__len__(self)

    def __getattr__(self, attribute):
        return getattr(self._internal_container, attribute)

    def setdefault(self, key: Union[Key, str], default: Any) -> Any:
        super().setdefault(key, default=default)
        return self[key]


def ends_with_withespace(it: Any) -> bool:
    """Returns ``True`` if the given item ``it`` is a ``Table`` or ``AoT`` object
    ending with a ``Whitespace``.
    """
    return (
        isinstance(it, Table) and isinstance(it.value._previous_item(), Whitespace)
    ) or (isinstance(it, AoT) and len(it) > 0 and isinstance(it[-1], Whitespace))
