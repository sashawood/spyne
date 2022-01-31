
#
# spyne - Copyright (C) Spyne contributors.
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301
#

import logging
logger = logging.getLogger(__name__)

import re

from collections import deque
from collections import defaultdict

from spyne.util import six
from spyne.error import ValidationError

from spyne.model import ByteArray, String, File, ComplexModelBase, Array, \
    SimpleModel, Any, AnyDict, Unicode

from spyne.protocol.dictdoc import DictDocument


RE_HTTP_ARRAY_INDEX = re.compile(r"\[([0-9]+)]")


def _s2cmi(m, nidx):
    """
    Sparse to contiguous mapping inserter.

    >>> m1={3:0, 4:1, 7:2}
    >>> _s2cmi(m1, 5); m1
    1
    {3: 0, 4: 1, 5: 2, 7: 3}
    >>> _s2cmi(m1, 0); m1
    0
    {0: 0, 3: 1, 4: 2, 5: 3, 7: 4}
    >>> _s2cmi(m1, 8); m1
    4
    {0: 0, 3: 1, 4: 2, 5: 3, 7: 4, 8: 5}
    """
    nv = -1
    for i, v in m.items():
        if i >= nidx:
            m[i] += 1
        elif v > nv:
            nv = v
    m[nidx] = nv + 1
    return nv + 1


def _fill(inst_class, frequencies):
    """This function initializes the frequencies dict with null values. If this
    is not done, it won't be possible to catch missing elements when validating
    the incoming document.
    """

    ctype_info = inst_class.get_flat_type_info(inst_class)
    cfreq_key = inst_class, 0

    for k, v in ctype_info.items():
        if v.Attributes.min_occurs > 0:
            frequencies[cfreq_key][k] = 0


class SimpleDictDocument(DictDocument):
    """This protocol contains logic for protocols that serialize and deserialize
    flat dictionaries. The only example as of now is Http.
    """

    def __init__(self, app=None, validator=None, mime_type=None,
                 ignore_uncap=False, ignore_wrappers=True, complex_as=dict,
                            ordered=False, hier_delim='.', strict_arrays=False):
        super(SimpleDictDocument, self).__init__(app=app, validator=validator,
                        mime_type=mime_type, ignore_uncap=ignore_uncap,
                        ignore_wrappers=ignore_wrappers, complex_as=complex_as,
                                                                ordered=ordered)

        self.hier_delim = hier_delim
        self.strict_arrays = strict_arrays

    def _to_native_values(self, cls, member, orig_k, k, v, req_enc, validator):
        value = []

        for v2 in v:
            # some wsgi implementations pass unicode strings, some pass str
            # strings. we get unicode here when we can and should.
            if v2 is not None and req_enc is not None \
                                    and not issubclass(member.type, String) \
                                    and issubclass(member.type, Unicode) \
                                    and not isinstance(v2, six.text_type):
                try:
                    v2 = v2.decode(req_enc)
                except UnicodeDecodeError as e:
                    raise ValidationError(v2, "%r while decoding %%r" % e)

            # validate raw data (before deserialization)
            try:
                if (validator is self.SOFT_VALIDATION and not
                                  member.type.validate_string(member.type, v2)):
                    raise ValidationError([orig_k, v2])

            except TypeError:
                raise ValidationError([orig_k, v2])

            cls_attrs = self.get_cls_attrs(member.type)
            v2 = self._parse(cls_attrs, v2)

            # deserialize to native type
            if issubclass(member.type, File):
                if isinstance(v2, File.Value):
                    native_v2 = v2
                else:
                    native_v2 = self.from_unicode(member.type, v2,
                                                           self.binary_encoding)

            elif issubclass(member.type, ByteArray):
                native_v2 = self.from_unicode(member.type, v2,
                                                           self.binary_encoding)
            else:
                try:
                    native_v2 = self.from_unicode(member.type, v2)
                except ValidationError as e:
                    ns = "%s.%s" % (cls.get_namespace(), cls.get_type_name())
                    raise ValidationError(e.faultstring,
                                  "Validation failed for %s.%s: %%s" % (ns, k))

            # validate native data (after deserialization)
            native_v2 = self._sanitize(cls_attrs, native_v2)
            if validator is self.SOFT_VALIDATION:
                if not member.type.validate_native(member.type, native_v2):
                    raise ValidationError([orig_k, v2])

            value.append(native_v2)

        return value

    def simple_dict_to_object(self, ctx, doc, cls, validator=None, req_enc=None):
        """Converts a flat dict to a native python object.

        See :func:`spyne.model.complex.ComplexModelBase.get_flat_type_info`.
        """

        if issubclass(cls, (Any, AnyDict)):
            return doc

        if not issubclass(cls, ComplexModelBase):
            raise NotImplementedError("Interestingly, deserializing non complex"
                                      " types is not yet implemented. You can"
                                      " use a ComplexModel to wrap that field."
                                      " Otherwise, patches are welcome.")

        # this is for validating cls.Attributes.{min,max}_occurs
        frequencies = defaultdict(lambda: defaultdict(int))
        if validator is self.SOFT_VALIDATION:
            _fill(cls, frequencies)

        if issubclass(cls, Array):
            # we need the wrapper object instance here as it's a root object
            retval = cls.get_serialization_instance([])
        else:
            retval = cls.get_deserialization_instance(ctx)

        simple_type_info = cls.get_simple_type_info_with_prot(cls, self,
                                                     hier_delim=self.hier_delim)

        logger.debug("Simple type info key: %r", simple_type_info.keys())

        idxmap = defaultdict(dict)
        for orig_k, v in sorted(doc.items(), key=lambda _k: _k[0]):
            k = RE_HTTP_ARRAY_INDEX.sub("", orig_k)

            member = simple_type_info.get(k, None)
            if member is None:
                logger.debug("\tdiscarding field %r" % k)
                continue

            if member.can_be_empty:
                if v != ['empty']:  # maybe raise a ValidationError instead?
                    # 'empty' is the only valid value at this point after all
                    continue

                assert issubclass(member.type, ComplexModelBase)

                if issubclass(member.type, Array):
                    value = []

                elif self.get_cls_attrs(member.type).max_occurs > 1:
                    value = []

                else:
                    value = [member.type.get_deserialization_instance(ctx)]
                    # do we have to ignore later assignments? they're illegal
                    # but not harmful.
            else:
                # extract native values from the list of strings in the flat dict
                # entries.
                value = self._to_native_values(cls, member, orig_k, k, v,
                                                             req_enc, validator)


            # assign the native value to the relevant class in the nested object
            # structure.
            cinst = retval
            ctype_info = cls.get_flat_type_info(cls)
            ccls_attr = self.get_cls_attrs(cls)
            value = self._cast(ccls_attr, value)

            idx, nidx = 0, 0
            pkey = member.path[0]
            cfreq_key = cls, idx

            indexes = deque(RE_HTTP_ARRAY_INDEX.findall(orig_k))

            for pkey in member.path[:-1]:
                nidx = 0
                ncls, ninst = ctype_info[pkey], getattr(cinst, pkey, None)
                nattrs = self.get_cls_attrs(ncls)
                if issubclass(ncls, Array):
                    ncls, = ncls._type_info.values()

                ncls_attrs = self.get_cls_attrs(ncls)
                mo = ncls_attrs.max_occurs
                if mo > 1:
                    if len(indexes) == 0:
                        nidx = 0
                    else:
                        nidx = int(indexes.popleft())

                    if ninst is None:
                        ninst = []
                        cinst._safe_set(pkey, ninst, ncls, nattrs)

                    if self.strict_arrays:
                        if len(ninst) == 0:
                            newval = ncls.get_deserialization_instance(ctx)
                            ninst.append(newval)
                            frequencies[cfreq_key][pkey] += 1

                        if nidx > len(ninst):
                            raise ValidationError(orig_k,
                                            "%%r Invalid array index %d." % idx)
                        if nidx == len(ninst):
                            ninst.append(ncls.get_deserialization_instance(ctx))
                            frequencies[cfreq_key][pkey] += 1

                        cinst = ninst[nidx]

                    else:
                        _m = idxmap[id(ninst)]
                        cidx = _m.get(nidx, None)
                        if cidx is None:
                            cidx = _s2cmi(_m, nidx)
                            newval = ncls.get_deserialization_instance(ctx)
                            ninst.insert(cidx, newval)
                            frequencies[cfreq_key][pkey] += 1
                        cinst = ninst[cidx]

                    assert cinst is not None, ninst

                else:
                    if ninst is None:
                        ninst = ncls.get_deserialization_instance(ctx)
                        cinst._safe_set(pkey, ninst, ncls, nattrs)
                        frequencies[cfreq_key][pkey] += 1

                    cinst = ninst

                cfreq_key = cfreq_key + (ncls, nidx)
                idx = nidx
                ctype_info = ncls.get_flat_type_info(ncls)

            frequencies[cfreq_key][member.path[-1]] += len(value)

            member_attrs = self.get_cls_attrs(member.type)
            if member_attrs.max_occurs > 1:
                _v = getattr(cinst, member.path[-1], None)
                is_set = True
                if _v is None:
                    is_set = cinst._safe_set(member.path[-1], value,
                                                      member.type, member_attrs)
                else:
                    _v.extend(value)

                set_skip = 'set ' if is_set else 'SKIP'
                logger.debug("\t%s arr %r(%r) = %r" %
                                           (set_skip, member.path, pkey, value))

            else:
                is_set = cinst._safe_set(member.path[-1], value[0],
                                                      member.type, member_attrs)

                set_skip = 'set ' if is_set else 'SKIP'
                logger.debug("\t%s val %r(%r) = %r" %
                                        (set_skip, member.path, pkey, value[0]))

        if validator is self.SOFT_VALIDATION:
            logger.debug("\tvalidate_freq: \n%r", frequencies)
            for k, d in frequencies.items():
                for i, path_cls in enumerate(k[:-1:2]):
                    attrs = self.get_cls_attrs(path_cls)
                    if not attrs.validate_freq:
                        logger.debug("\t\tskip validate_freq: %r", k[:i*2])
                        break
                else:
                    path_cls = k[-2]
                    logger.debug("\t\tdo validate_freq: %r", k)
                    self._check_freq_dict(path_cls, d)

        if issubclass(cls, Array):
            # unwrap the request object
            array_name, = cls._type_info.keys()
            retval = getattr(retval, array_name)

        return retval

    def object_to_simple_dict(self, cls, inst, retval=None,
                   prefix=None, subinst_eater=lambda prot, v, t: v, tags=None):
        """Converts a native python object to a flat dict.

        See :func:`spyne.model.complex.ComplexModelBase.get_flat_type_info`.
        """

        if retval is None:
            retval = {}

        if prefix is None:
            prefix = []

        if inst is None and self.get_cls_attrs(cls).min_occurs == 0:
            return retval

        if tags is None:
            tags = set([id(inst)])
        else:
            if id(inst) in tags:
                return retval

        if issubclass(cls, ComplexModelBase):
            fti = cls.get_flat_type_info(cls)

            for k, v in fti.items():
                new_prefix = list(prefix)
                cls_attrs = self.get_cls_attrs(v)
                sub_name = cls_attrs.sub_name
                if sub_name is None:
                    sub_name = k
                new_prefix.append(sub_name)
                subinst = getattr(inst, k, None)

                if (issubclass(v, Array) or v.Attributes.max_occurs > 1) and \
                                                            subinst is not None:
                    if issubclass(v, Array):
                        subtype, = v._type_info.values()
                    else:
                        subtype = v

                    # for simple types, the same key is repeated with multiple
                    # values
                    if issubclass(subtype, SimpleModel):
                        key = self.hier_delim.join(new_prefix)
                        l = []
                        for ssv in subinst:
                            l.append(subinst_eater(self, ssv, subtype))
                        retval[key] = l

                    else:
                        # for complex types, brackets are used for each value.
                        last_prefix = new_prefix[-1]
                        i = -1
                        for i, ssv in enumerate(subinst):
                            new_prefix[-1] = '%s[%d]' % (last_prefix, i)
                            self.object_to_simple_dict(subtype, ssv,
                                    retval, new_prefix,
                                         subinst_eater=subinst_eater, tags=tags)

                        if i == -1:
                            key = self.hier_delim.join(new_prefix)
                            retval[key] = 'empty'

                else:
                    self.object_to_simple_dict(v, subinst, retval, new_prefix,
                                         subinst_eater=subinst_eater, tags=tags)

        else:
            key = self.hier_delim.join(prefix)

            if key in retval:
                raise ValueError("%r.%s conflicts with previous value %r" %
                                                        (cls, key, retval[key]))

            retval[key] = subinst_eater(self, inst, cls)

        return retval
