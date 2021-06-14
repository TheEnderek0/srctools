"""Read and write parts of Source BSP files.

Data from a read BSP is lazily parsed when each section is accessed.
"""
from typing import (
    overload, TypeVar, Any, Generic, Union, Optional, ClassVar, Type,
    List, Iterator, BinaryIO, Tuple, Callable, Dict
)
from io import BytesIO
from enum import Enum, Flag
from zipfile import ZipFile
import itertools
import struct
import contextlib
import warnings
import attr

from srctools import AtomicWriter, Vec, Angle, conv_int
from srctools.vmf import VMF, Entity, Output
from srctools.tokenizer import escape_text
from srctools.binformat import struct_read, DeferredWrites
from srctools.property_parser import Property

__all__ = [
    'BSP_LUMPS', 'VERSIONS',
    'BSP', 'Lump',
    'StaticProp', 'StaticPropFlags',
    'Cubemap', 'Overlay',
    'VisTree', 'VisLeaf',
]


BSP_MAGIC = b'VBSP'  # All BSP files start with this
HEADER_1 = '<4si'  # Header section before the lump list.
HEADER_LUMP = '<3i4s'  # Header section for each lump.
HEADER_2 = '<i'  # Header section after the lumps.
T = TypeVar('T')


class VERSIONS(Enum):
    """The BSP version numbers for various games."""
    VER_17 = 17
    VER_18 = 18
    VER_19 = 19
    VER_20 = 20
    VER_21 = 21
    VER_22 = 22
    VER_29 = 29

    HL2 = 19
    CS_SOURCE = 19
    DOF_SOURCE = 19
    HL2_EP1 = 20
    HL2_EP2 = 20
    HL2_LC = 20

    GARYS_MOD = 20
    TF2 = 20
    PORTAL = 20
    L4D = 20
    ZENO_CLASH = 20
    DARK_MESSIAH = 20
    VINDICTUS = 20
    THE_SHIP = 20

    BLOODY_GOOD_TIME = 20
    L4D2 = 21
    ALIEN_SWARM = 21
    PORTAL_2 = 21
    CS_GO = 21
    DEAR_ESTHER = 21
    STANLEY_PARABLE = 21
    DOTA2 = 22
    CONTAGION = 23

    DESOLATION = 42

    def __eq__(self, other: object):
        """Versions are equal to their integer value."""
        return self.value == other


class BSP_LUMPS(Enum):
    """All the lumps in a BSP file.

    The values represent the order lumps appear in the index.
    Some indexes were reused, so they have aliases.
    """
    ENTITIES = 0
    PLANES = 1
    TEXDATA = 2
    VERTEXES = 3
    VISIBILITY = 4
    NODES = 5
    TEXINFO = 6
    FACES = 7
    LIGHTING = 8
    OCCLUSION = 9
    LEAFS = 10
    FACEIDS = 11
    EDGES = 12
    SURFEDGES = 13
    MODELS = 14
    WORLDLIGHTS = 15
    LEAFFACES = 16
    LEAFBRUSHES = 17
    BRUSHES = 18
    BRUSHSIDES = 19
    AREAS = 20
    AREAPORTALS = 21

    PORTALS = 22
    UNUSED0 = 22
    PROPCOLLISION = 22

    CLUSTERS = 23
    UNUSED1 = 23
    PROPHULLS = 23

    PORTALVERTS = 24
    UNUSED2 = 24
    PROPHULLVERTS = 24

    CLUSTERPORTALS = 25
    UNUSED3 = 25
    PROPTRIS = 25

    DISPINFO = 26
    ORIGINALFACES = 27
    PHYSDISP = 28
    PHYSCOLLIDE = 29
    VERTNORMALS = 30
    VERTNORMALINDICES = 31
    DISP_LIGHTMAP_ALPHAS = 32
    DISP_VERTS = 33
    DISP_LIGHTMAP_SAMPLE_POSITIONS = 34
    GAME_LUMP = 35
    LEAFWATERDATA = 36
    PRIMITIVES = 37
    PRIMVERTS = 38
    PRIMINDICES = 39
    PAKFILE = 40
    CLIPPORTALVERTS = 41
    CUBEMAPS = 42
    TEXDATA_STRING_DATA = 43
    TEXDATA_STRING_TABLE = 44
    OVERLAYS = 45
    LEAFMINDISTTOWATER = 46
    FACE_MACRO_TEXTURE_INFO = 47
    DISP_TRIS = 48
    PROP_BLOB = 49
    PHYSCOLLIDESURFACE = 49
    WATEROVERLAYS = 50

    LEAF_AMBIENT_INDEX_HDR = 51
    LEAF_AMBIENT_INDEX = 52

    LIGHTMAPPAGES = 51
    LIGHTMAPPAGEINFOS = 52

    LIGHTING_HDR = 53
    WORLDLIGHTS_HDR = 54
    LEAF_AMBIENT_LIGHTING_HDR = 55
    LEAF_AMBIENT_LIGHTING = 56
    XZIPPAKFILE = 57
    FACES_HDR = 58
    MAP_FLAGS = 59
    OVERLAY_FADES = 60
    OVERLAY_SYSTEM_LEVELS = 61
    PHYSLEVEL = 62
    DISP_MULTIBLEND = 63

LUMP_COUNT = max(lump.value for lump in BSP_LUMPS) + 1  # 64 normally

# Special-case the packfile lump, put it at the end.
# This way the BSP can be opened by generic zip programs.
LUMP_WRITE_ORDER = list(BSP_LUMPS)
LUMP_WRITE_ORDER.remove(BSP_LUMPS.PAKFILE)
LUMP_WRITE_ORDER.append(BSP_LUMPS.PAKFILE)

# When remaking the lumps from trees of objects,
# they need to be done in the correct order so stuff referring
# to other trees can ad their data.
LUMP_REBUILD_ORDER = [
    # No dependencies.
    BSP_LUMPS.PAKFILE,
    BSP_LUMPS.ENTITIES,
    BSP_LUMPS.CUBEMAPS,

    BSP_LUMPS.OVERLAYS,  # Adds texinfo entries.

    BSP_LUMPS.TEXINFO,  # Adds texdata -> texdata_string_data entries.
    BSP_LUMPS.TEXDATA_STRING_DATA,
]


class SurfFlags(Flag):
    """The various SURF_ flags, indicating different attributes for faces."""
    NONE = 0
    LIGHT = 0x1  # The face has lighting info.
    SKYBOX_2D = 0x2  # Nodraw, but when visible 2D skybox should be rendered.
    SKYBOX_3D = 0x4  # Nodraw, but when visible 2D and 3D skybox should be rendered.
    WATER_WARP = 0x8  # 'turbulent water warp'
    TRANSLUCENT = 0x10  # Translucent material.
    NOPORTAL = 0x20  # Portalgun blocking material.
    TRIGGER = 0x40  # XBox only - is a trigger surface.
    NODRAW = 0x80  # Texture isn't used, it's invisible.
    HINT = 0x100 # A hint brush.
    SKIP = 0x200  # Skip brush, removed from map.
    NOLIGHT = 0x400  # No light needs to be calulated.
    BUMPLIGHT = 0x800  # Needs three lightmaps for bumpmapping.
    NO_SHADOWS = 0x1000  # Doesn't recive shadows.
    NO_DECALS = 0x2000  # Rejects decals.
    NO_SUBDIVIDE = 0x4000  # Not allowed to split up the brush face.
    HITBOX = 0x8000  # 'Part of a hitbox'


class StaticPropFlags(Flag):
    """Bitflags specified for static props."""
    NONE = 0

    DOES_FADE = 0x01  # Is the fade distances set?
    HAS_LIGHTING_ORIGIN = 0x02  # info_lighting entity used.
    DISABLE_DRAW = 0x04  # Changes at runtime.
    IGNORE_NORMALS = 0x08
    NO_SHADOW = 0x10
    SCREEN_SPACE_FADE = 0x20  # Use screen space fading. Obsolete since at least ASW.
    NO_PER_VERTEX_LIGHTING = 0x40
    NO_SELF_SHADOWING = 0x80

    # These are set in the secondary flags section.
    NO_FLASHLIGHT = 0x100  # Disable projected texture lighting.
    BOUNCED_LIGHTING = 0x0400  # Bounce lighting off the prop.

    @property
    def value_prim(self) -> int:
        """Return the data for the original flag byte."""
        return self.value & 0xFF

    @property
    def value_sec(self) -> int:
        """Return the data for the secondary flag byte."""
        return self.value >> 8


class ParsedLump(Generic[T]):
    """Allows access to parsed versions of lumps.
    
    When accessed, the corresponding lump is parsed into an object tree.
    The lump is then cleared of data.
    When the BSP is saved, the lump data is then constructed.
    """

    def __init__(self, lump: BSP_LUMPS, *extra: BSP_LUMPS) -> None:
        self.lump = lump
        self.to_clear = (lump, ) + extra
        self.__name__ = ''
        self._read: Optional[Callable[[BSP, bytes], T]] = None
        self._check: Optional[Callable[[BSP, T], None]] = None
        assert self.lump in LUMP_REBUILD_ORDER, self.lump

    def __set_name__(self, owner: Type['BSP'], name: str) -> None:
        self.__name__ = name
        self.__objclass__ = owner
        self._read = getattr(owner, '_lmp_read_' + name)
        self._check = getattr(owner, '_lmp_check_' + name, None)
        # noinspection PyProtectedMember
        owner._save_funcs[self.lump] = getattr(owner, '_lmp_write_' + name)

    def __repr__(self) -> str:
        return f'<srctools.BSP.{self.__name__} member>'
        
    @overload
    def __get__(self, instance: None, owner=None) -> 'ParsedLump[T]': ...
    @overload
    def __get__(self, instance: 'BSP', owner=None) -> T: ...
        
    def __get__(self, instance: Optional['BSP'], owner=None) -> Union['ParsedLump', T]:
        """Read the lump, then discard."""
        if instance is None:  # Accessed on the class.
            return self
        try:
            return instance._parsed_lumps[self.lump]  # noqa
        except KeyError:
            pass
        if self._read is None:
            raise TypeError('ParsedLump.__set_name__ was never called!')
        
        data = instance.lumps[self.lump].data
        result = self._read(instance, data)
        instance._parsed_lumps[self.lump] = result # noqa
        for lump in self.to_clear:
            instance.lumps[lump].data = b''
        return result

    def __set__(self, instance: Optional['BSP'], value: T) -> None:
        """Discard lump data, then store."""

        if instance is None:
            raise TypeError('Cannot assign directly to lump descriptor!')
        if self._check is not None:
            # Allow raising if an invalid value.
            self._check(instance, value)
        for lump in self.to_clear:
            instance.lumps[lump].data = b''
        instance._parsed_lumps[self.lump] = value  # noqa


class BSP:
    """A BSP file."""
    # Parsed lump -> func which remakes the raw data. Any = ParsedLump's T, but
    # that can't bind here.
    _save_funcs: ClassVar[Dict[BSP_LUMPS, Callable[['BSP', Any], bytes]]] = {}
    def __init__(self, filename: str, version: VERSIONS=None):
        self.filename = filename
        self.map_revision = -1  # The map's revision count
        self.lumps = {}  # type: dict[BSP_LUMPS, Lump]
        self._parsed_lumps = {}  # type: dict[BSP_LUMPS, Any]
        self.game_lumps = {}  # type: dict[bytes, GameLump]
        self.header_off = 0
        self.version = version  # type: Optional[Union[VERSIONS, int]]
        # Tracks if the ent lump is using the new x1D output separators,
        # or the old comma separators. If no ouputs are present there's no
        # way to determine this.
        self.out_comma_sep: Optional[bool] = None
        # This internally stores the texdata values texinfo refers to. Users
        # don't interact directly, instead they use the create_texinfo / texinfo.set()
        # methods that create the data as required.
        self._texdata: Dict[str, TexData] = {}

        self.read()

    pakfile: ParsedLump[ZipFile] = ParsedLump(BSP_LUMPS.PAKFILE)
    ents: ParsedLump[VMF] = ParsedLump(BSP_LUMPS.ENTITIES)
    textures: ParsedLump[List[str]] = ParsedLump(BSP_LUMPS.TEXDATA_STRING_DATA, BSP_LUMPS.TEXDATA_STRING_TABLE)
    texinfo: ParsedLump[List['TexInfo']] = ParsedLump(BSP_LUMPS.TEXINFO, BSP_LUMPS.TEXDATA)
    cubemaps: ParsedLump[List['Cubemap']] = ParsedLump(BSP_LUMPS.CUBEMAPS)
    overlays: ParsedLump[List['Overlay']] = ParsedLump(BSP_LUMPS.OVERLAYS)

    def read(self) -> None:
        """Load all data."""
        self.lumps.clear()
        self.game_lumps.clear()

        with open(self.filename, mode='br') as file:
            # BSP files start with 'VBSP', then a version number.
            magic_name, bsp_version = struct_read(HEADER_1, file)
            assert magic_name == BSP_MAGIC, 'Not a BSP file!'

            if self.version is None:
                try:
                    self.version = VERSIONS(bsp_version)
                except ValueError:
                    self.version = bsp_version
            else:
                assert bsp_version == self.version, 'Different BSP version!'

            lump_offsets = {}

            # Read the index describing each BSP lump.
            for index in range(LUMP_COUNT):
                offset, length, version, ident = struct_read(HEADER_LUMP, file)
                lump_id = BSP_LUMPS(index)
                self.lumps[lump_id] = Lump(
                    lump_id,
                    version,
                    ident,
                )
                lump_offsets[lump_id] = offset, length

            [self.map_revision] = struct_read(HEADER_2, file)

            for lump in self.lumps.values():
                # Now read in each lump.
                offset, length = lump_offsets[lump.type]
                file.seek(offset)
                lump.data = file.read(length)

            game_lump = self.lumps[BSP_LUMPS.GAME_LUMP]

            self.game_lumps.clear()

            [lump_count] = struct.unpack_from('<i', game_lump.data)
            lump_offset = 4

            for _ in range(lump_count):
                (
                    game_lump_id,
                    flags,
                    glump_version,
                    file_off,
                    file_len,
                ) = GameLump.ST.unpack_from(game_lump.data, lump_offset)  # type: bytes, int, int, int, int
                lump_offset += GameLump.ST.size

                file.seek(file_off)

                # The lump ID is backward..
                game_lump_id = game_lump_id[::-1]

                self.game_lumps[game_lump_id] = GameLump(
                    game_lump_id,
                    flags,
                    glump_version,
                    file.read(file_len),
                )
            # This is not valid any longer.
            game_lump.data = b''

    def save(self, filename=None) -> None:
        """Write the BSP back into the given file."""
        # First, go through lumps the user has accessed, and rebuild their data.
        for lump in LUMP_REBUILD_ORDER:
            try:
                data = self._parsed_lumps.pop(lump)
            except KeyError:
                pass
            else:
                self.lumps[lump].data = self._save_funcs[lump](self, data)
        game_lumps = list(self.game_lumps.values())  # Lock iteration order.

        with AtomicWriter(filename or self.filename, is_bytes=True) as file:  # type: BinaryIO
            # Needed to allow writing out the header before we know the position
            # data will be.
            defer = DeferredWrites(file)

            if isinstance(self.version, VERSIONS):
                version = self.version.value
            else:
                version = self.version

            file.write(struct.pack(HEADER_1, BSP_MAGIC, version))

            # Write headers.
            for lump_name in BSP_LUMPS:
                lump = self.lumps[lump_name]
                defer.defer(lump_name, '<ii')
                file.write(struct.pack(
                    HEADER_LUMP,
                    0,  # offset
                    0,  # length
                    lump.version,
                    lump.ident,
                ))

            # After lump headers, the map revision...
            file.write(struct.pack(HEADER_2, self.map_revision))

            # Then each lump.
            for lump_name in LUMP_WRITE_ORDER:
                # Write out the actual data.
                lump = self.lumps[lump_name]
                if lump_name is BSP_LUMPS.GAME_LUMP:
                    # Construct this right here.
                    lump_start = file.tell()
                    file.write(struct.pack('<i', len(game_lumps)))
                    for game_lump in game_lumps:
                        file.write(struct.pack(
                            '<4s HH',
                            game_lump.id[::-1],
                            game_lump.flags,
                            game_lump.version,
                        ))
                        defer.defer(game_lump.id, '<i', write=True)
                        file.write(struct.pack('<i', len(game_lump.data)))

                    # Now write data.
                    for game_lump in game_lumps:
                        defer.set_data(game_lump.id, file.tell())
                        file.write(game_lump.data)
                    # Length of the game lump is current - start.
                    defer.set_data(
                        lump_name,
                        lump_start,
                        file.tell() - lump_start,
                    )
                else:
                    # Normal lump.
                    defer.set_data(lump_name, file.tell(), len(lump.data))
                    file.write(lump.data)
            # Apply all the deferred writes.
            defer.write()

    def read_header(self) -> None:
        """No longer used."""

    def read_game_lumps(self) -> None:
        """No longer used."""

    def replace_lump(
        self,
        new_name: str,
        lump: Union[BSP_LUMPS, 'Lump'],
        new_data: bytes
    ) -> None:
        """Write out the BSP file, replacing a lump with the given bytes.

        This is deprecated, simply assign to the .data attribute of the lump.
        """
        if isinstance(lump, BSP_LUMPS):
            lump = self.lumps[lump]

        lump.data = new_data

        self.save(new_name)

    def get_lump(self, lump: BSP_LUMPS) -> bytes:
        """Return the contents of the given lump."""
        return self.lumps[lump].data

    def get_game_lump(self, lump_id: bytes) -> bytes:
        """Get a given game-lump, given the 4-character byte ID."""
        try:
            lump = self.game_lumps[lump_id]
        except KeyError:
            raise ValueError('{!r} not in {}'.format(lump_id, list(self.game_lumps)))
        return lump.data

    # Lump-specific commands:

    def read_texture_names(self) -> Iterator[str]:
        """Iterate through all brush textures in the map."""
        warnings.warn('Access bsp.textures', DeprecationWarning, stacklevel=2)
        return iter(self.textures)

    def _lmp_read_textures(self, tex_data: bytes) -> List[str]:
        tex_table = self.get_lump(BSP_LUMPS.TEXDATA_STRING_TABLE)
        # tex_table is an array of int offsets into tex_data. tex_data is a
        # null-terminated block of strings.

        table_offsets = struct.unpack(
            # The number of ints + i, for the repetitions in the struct.
            '<' + str(len(tex_table) // struct.calcsize('i')) + 'i',
            tex_table,
        )

        mat_list = []

        for off in table_offsets:
            # Look for the NULL at the end - strings are limited to 128 chars.
            str_off = 0
            for str_off in range(off, off + 128):
                if tex_data[str_off] == 0:
                    mat_list.append(tex_data[off: str_off].decode('ascii'))
                    break
            else:
                # Reached the 128 char limit without finding a null.
                raise ValueError(f'Bad string at {off} in BSP! ({tex_data[off:str_off]!r})')
        return mat_list

    def _lmp_write_textures(self, textures: List[str]) -> bytes:
        table = BytesIO()
        data = bytearray()
        for tex in textures:
            if len(tex) >= 128:
                raise OverflowError(f'Texture "{tex}" exceeds 128 character limit')
            string = tex.encode('ascii') + b'\0'
            ind = data.find(string)
            if ind == -1:
                ind = len(data)
                data.extend(string)
            table.write(struct.pack('<i', ind))
        self.lumps[BSP_LUMPS.TEXDATA_STRING_TABLE].data = table.getvalue()
        return bytes(data)

    def _lmp_read_texinfo(self, data: bytes) -> List['TexInfo']:
        """Read the texture info lump, providing positioning information."""
        texdata_list = []
        for (
            ref_x, ref_y, ref_z, ind,
            w, h, vw, vh,
        ) in struct.iter_unpack('<3f5i', self.lumps[BSP_LUMPS.TEXDATA].data):
            mat = self.textures[ind]
            # The view width/height is unused stuff, identical to regular
            # width/height.
            assert vw == w and vh == h, f'{mat}: {w}x{h} != view {vw}x{vh}'
            texdata = TexData(mat, Vec(ref_x, ref_y, ref_z), w, h)
            texdata_list.append(texdata)
            self._texdata[mat.casefold()] = texdata
        self.lumps[BSP_LUMPS.TEXDATA].data = b''

        return [
            TexInfo(
                Vec(sx, sy, sz), so,
                Vec(tx, ty, tz), to,
                Vec(l_sx, l_sy, l_sz), l_so,
                Vec(l_tx, l_ty, l_tz), l_to,
                SurfFlags(flags),
                texdata_list[texdata_ind],
            )
            for (
                sx, sy, sz, so, tx, ty, tz, to,
                l_sx, l_sy, l_sz, l_so, l_tx, l_ty, l_tz, l_to,
                flags,
                texdata_ind
            ) in struct.iter_unpack('<16fii', data)
        ]

    def _lmp_write_texinfo(self, texinfos: List['TexInfo']) -> bytes:
        """Rebuild the texinfo and texdata lump."""
        # Map texture name or the texdata block to the index in the resultant list.
        texture_ind = {
            mat.casefold(): i
            for i, mat in enumerate(self.textures)
        }
        texdata_ind = {}

        texdata_list = []
        texinfo_result = []

        for info in texinfos:
            # noinspection PyProtectedMember
            tdat = info._info
            # Try and find an existing reference to this texdata.
            # If not, the index is at the end of the list, where we write and
            # insert it.
            # That's then done again for the texture name itself.
            try:
                ind = texdata_ind[tdat]
            except KeyError:
                ind = texdata_ind[tdat] = len(texdata_list)
                try:
                    tex_ind = texture_ind[tdat.mat.casefold()]
                except KeyError:
                    tex_ind = len(self.textures)
                    self.textures.append(tdat.mat)

                texdata_list.append(struct.pack(
                    '<3f5i',
                    tdat.reflectivity.x, tdat.reflectivity.y, tdat.reflectivity.z,
                    tex_ind,
                    tdat.width, tdat.height, tdat.width, tdat.height,
                ))
            texinfo_result.append(struct.pack(
                '<16fii',
                *info.s_off, info.s_shift,
                *info.t_off, info.t_shift,
                *info.lightmap_s_off, info.lightmap_s_shift,
                *info.lightmap_t_off, info.lightmap_t_shift,
                info.flags.value,
                ind,
            ))
        self.lumps[BSP_LUMPS.TEXDATA].data = b''.join(texdata_list)
        return b''.join(texinfo_result)

    def _lmp_read_pakfile(self, data: bytes) -> ZipFile:
        """Read the raw binary as writable zip archive."""
        zipfile = ZipFile(BytesIO(data), mode='a')
        zipfile.filename = self.filename
        return zipfile

    def _lmp_write_pakfile(self, file: ZipFile) -> bytes:
        """Extract the final zip data from the zipfile."""
        # Explicitly close the zip file, so the footer is done.
        buf = file.fp
        file.close()
        if isinstance(buf, BytesIO):
            return buf.getvalue()
        else:
            buf.seek(0)
            return buf.read()

    def _lmp_check_pakfile(self, file: ZipFile) -> None:
        if not isinstance(file.fp, BytesIO):
            raise ValueError('Zipfiles must be constructed with a BytesIO buffer.')

    def _lmp_read_cubemaps(self, data: bytes) -> List['Cubemap']:
        """Read the cubemaps lump."""
        return [
            Cubemap(Vec(x, y, z), size)
            for (x, y, z, size) in struct.iter_unpack('<iiii', data)
        ]

    def _lmp_write_cubemaps(self, cubemaps: List['Cubemap']) -> bytes:
        """Write out the cubemaps lump."""
        return b''.join([
            struct.pack(
                '<iiii',
                int(round(cube.origin.x)),
                int(round(cube.origin.y)),
                int(round(cube.origin.z)),
                cube.size,
            )
            for cube in cubemaps
        ])

    def _lmp_read_overlays(self, data: bytes) -> List['Overlay']:
        """Read the overlays lump."""
        overlays = []
        #
        for block in struct.iter_unpack(
            '<ihH'  # id, texinfo, face-and-render-order
            '64i'  # face array.
            '4f'  # UV min/max
            '18f',  # 4 handle points, origin, normal
            data,
        ):
            over_id, texinfo, face_ro = block[:3]
            face_count = face_ro & ((1 << 14) - 1)
            render_order = face_ro >> 14
            if face_count > 64:
                raise ValueError(f'{face_ro} exceeds OVERLAY_BSP_FACE_COUNT (64)!')
            faces = list(block[3: 3 + face_count])
            u_min, u_max, v_min, v_max = block[67:71]
            uv1 = Vec(block[71:74])
            uv2 = Vec(block[74:77])
            uv3 = Vec(block[77:80])
            uv4 = Vec(block[80:83])
            origin = Vec(block[83:86])
            normal = Vec(block[86:89])
            assert len(block) == 89
            overlays.append(Overlay(
                over_id, origin, normal,
                self.texinfo[texinfo], face_count,
                faces, render_order,
                u_min,
                u_max,
                v_min,
                v_max,
                uv1, uv2, uv3, uv4,
            ))
        return overlays

    def _lmp_write_overlays(self, over: List['Overlay']) -> bytes:
        raise NotImplementedError

    @contextlib.contextmanager
    def packfile(self) -> Iterator[ZipFile]:
        """A context manager to allow editing the packed content.

        When successfully exited, the zip will be rewritten to the BSP file.
        """
        warnings.warn('Use BSP.pakfile to access the cached archive.', DeprecationWarning, stacklevel=2)
        pak_lump = self.lumps[BSP_LUMPS.PAKFILE]
        data_file = BytesIO(pak_lump.data)

        zip_file = ZipFile(data_file, mode='a')
        # If exception, abort, so we don't need try: or with:.
        # Because this is purely in memory, there are no actual resources here
        # and we don't actually care about not closing the zip file or BytesIO.
        yield zip_file
        # Explicitly close to finalise the footer.
        zip_file.close()
        # Note: because data is bytes, CPython won't end up doing any copying
        # here.
        pak_lump.data = data_file.getvalue()

    def read_ent_data(self) -> VMF:
        warnings.warn('Use BSP.ents directly.', DeprecationWarning, stacklevel=2)
        return self._lmp_read_ents(self.get_lump(BSP_LUMPS.ENTITIES))

    def _lmp_read_ents(self, ent_data: bytes) -> VMF:
        """Parse in entity data.

        This returns a VMF object, with entities mirroring that in the BSP.
        No brushes are read.
        """
        vmf = VMF()
        cur_ent = None  # None when between brackets.
        seen_spawn = False  # The first entity is worldspawn.
        
        # This code performs the same thing as property_parser, but simpler
        # since there's no nesting, comments, or whitespace, except between
        # key and value. We also operate directly on the (ASCII) binary.
        for line in ent_data.splitlines():
            if line == b'{':
                if cur_ent is not None:
                    raise ValueError(
                        '2 levels of nesting after {} ents'.format(
                            len(vmf.entities)
                        )
                    )
                if not seen_spawn:
                    cur_ent = vmf.spawn
                    seen_spawn = True
                else:
                    cur_ent = Entity(vmf)
                continue
            elif line == b'}':
                if cur_ent is None:
                    raise ValueError(
                        f'Too many closing brackets after'
                        f' {len(vmf.entities)} ents!'
                    )
                if cur_ent is vmf.spawn:
                    if cur_ent['classname'] != 'worldspawn':
                        raise ValueError('No worldspawn entity!')
                else:
                    # The spawn ent is stored in the attribute, not in the ent
                    # list.
                    vmf.add_ent(cur_ent)
                cur_ent = None
                continue
            elif line == b'\x00':  # Null byte at end of lump.
                if cur_ent is not None:
                    raise ValueError("Last entity didn't end!")
                return vmf

            if cur_ent is None:
                raise ValueError("Keyvalue outside brackets!")

            # Line is of the form <"key" "val">, but handle escaped quotes
            # in the value. Valve's parser doesn't allow that, but we might
            # as well be better...
            key, value = line.split(b'" "', 2)
            decoded_key = key[1:].decode('ascii')
            decoded_value = value[:-1].replace(br'\"', b'"').decode('ascii')

            # Now, we need to figure out if this is a keyvalue,
            # or connection.
            # If we're L4D+, this is easy - they use 0x1D as separator.
            # Before, it's a comma which is common in keyvalues.
            # Assume it's an output if it has exactly 4 commas, and the last two
            # successfully parse as numbers.
            if 27 in value:
                # All outputs use the comma_sep, so we can ID them.
                cur_ent.add_out(Output.parse(Property(decoded_key, decoded_value)))
                if self.out_comma_sep is None:
                    self.out_comma_sep = False
            elif value.count(b',') == 4:
                try:
                    cur_ent.add_out(Output.parse(Property(decoded_key, decoded_value)))
                except ValueError:
                    cur_ent[decoded_key] = decoded_value
                if self.out_comma_sep is None:
                    self.out_comma_sep = True
            else:
                # Normal keyvalue.
                cur_ent[decoded_key] = decoded_value

        # This keyvalue needs to be stored in the VMF object too.
        # The one in the entity is ignored.
        vmf.map_ver = conv_int(vmf.spawn['mapversion'], vmf.map_ver)

        return vmf

    def _lmp_write_ents(self, vmf: VMF) -> bytes:
        return self.write_ent_data(vmf, self.out_comma_sep, _show_dep=False)

    @staticmethod
    def write_ent_data(vmf: VMF, use_comma_sep: Optional[bool]=None, *, _show_dep=True) -> bytes:
        """Generate the entity data lump.
        
        This accepts a VMF file like that returned from read_ent_data(). 
        Brushes are ignored, so the VMF must use *xx model references.

        use_comma_sep can be used to force using either commas, or 0x1D in I/O.
        """
        if _show_dep:
            warnings.warn('Modify BSP.ents instead', DeprecationWarning, stacklevel=2)
        out = BytesIO()
        for ent in itertools.chain([vmf.spawn], vmf.entities):
            out.write(b'{\n')
            for key, value in ent.keys.items():
                out.write('"{}" "{}"\n'.format(key, escape_text(value)).encode('ascii'))
            for output in ent.outputs:
                if use_comma_sep is not None:
                    output.comma_sep = use_comma_sep
                out.write(output._get_text().encode('ascii'))
            out.write(b'}\n')
        out.write(b'\x00')

        return out.getvalue()

    def static_prop_models(self) -> Iterator[str]:
        """Yield all model filenames used in static props."""
        static_lump = BytesIO(self.get_game_lump(b'sprp'))
        return self._read_static_props_models(static_lump)

    @staticmethod
    def _read_static_props_models(static_lump: BytesIO) -> Iterator[str]:
        """Read the static prop dictionary from the lump."""
        [dict_num] = struct_read('<i', static_lump)
        for _ in range(dict_num):
            [padded_name] = struct_read('<128s', static_lump)
            # Strip null chars off the end, and convert to a str.
            yield padded_name.rstrip(b'\x00').decode('ascii')

    def static_props(self) -> Iterator['StaticProp']:
        """Read in the Static Props lump."""
        # The version of the static prop format - different features.
        try:
            version = self.game_lumps[b'sprp'].version
        except KeyError:
            raise ValueError('No static prop lump!') from None

        if version > 11:
            raise ValueError('Unknown version ({})!'.format(version))
        if version < 4:
            # Predates HL2...
            raise ValueError('Static prop version {} is too old!')

        static_lump = BytesIO(self.game_lumps[b'sprp'].data)

        # Array of model filenames.
        model_dict = list(self._read_static_props_models(static_lump))

        [visleaf_count] = struct_read('<i', static_lump)
        visleaf_list = list(struct_read('H' * visleaf_count, static_lump))

        [prop_count] = struct_read('<i', static_lump)

        for i in range(prop_count):
            origin = Vec(struct_read('fff', static_lump))
            angles = Angle(struct_read('fff', static_lump))

            [model_ind] = struct_read('<H', static_lump)

            (
                first_leaf,
                leaf_count,
                solidity,
                flags,
                skin,
                min_fade,
                max_fade,
            ) = struct_read('<HHBBiff', static_lump)

            model_name = model_dict[model_ind]

            visleafs = visleaf_list[first_leaf:first_leaf + leaf_count]
            lighting_origin = Vec(struct_read('<fff', static_lump))

            if version >= 5:
                fade_scale = struct_read('<f', static_lump)[0]
            else:
                fade_scale = 1  # default

            if version in (6, 7):
                min_dx_level, max_dx_level = struct_read('<HH', static_lump)
            else:
                # Replaced by GPU & CPU in later versions.
                min_dx_level = max_dx_level = 0  # None

            if version >= 8:
                (
                    min_cpu_level,
                    max_cpu_level,
                    min_gpu_level,
                    max_gpu_level,
                ) = struct_read('BBBB', static_lump)
            else:
                # None
                min_cpu_level = max_cpu_level = 0
                min_gpu_level = max_gpu_level = 0

            if version >= 7:
                r, g, b, renderfx = struct_read('BBBB', static_lump)
                # Alpha isn't used.
                tint = Vec(r, g, b)
            else:
                # No tint.
                tint = Vec(255, 255, 255)
                renderfx = 255

            if version >= 11:
                # Unknown data, though it's float-like.
                unknown_1 = struct_read('<i', static_lump)

            if version >= 10:
                # Extra flags, post-CSGO.
                flags |= struct_read('<I', static_lump)[0] << 8

            flags = StaticPropFlags(flags)

            scaling = 1.0
            disable_on_xbox = False

            if version >= 11:
                # XBox support was removed. Instead this is the scaling factor.
                [scaling] = struct_read("<f", static_lump)
            elif version >= 9:
                # The single boolean byte also produces 3 pad bytes.
                [disable_on_xbox] = struct_read('<?xxx', static_lump)

            yield StaticProp(
                model_name,
                origin,
                angles,
                scaling,
                visleafs,
                solidity,
                flags,
                skin,
                min_fade,
                max_fade,
                lighting_origin,
                fade_scale,
                min_dx_level,
                max_dx_level,
                min_cpu_level,
                max_cpu_level,
                min_gpu_level,
                max_gpu_level,
                tint,
                renderfx,
                disable_on_xbox,
            )

    def write_static_props(self, props: List['StaticProp']) -> None:
        """Remake the static prop lump."""

        # First generate the visleaf and model-names block.
        # Unfortunately it seems reusing visleaf parts isn't possible.
        leaf_array = []  # type: List[int]
        leaf_offsets = []  # type: List[int]

        models = set()

        for prop in props:
            leaf_offsets.append(len(leaf_array))
            leaf_array.extend(prop.visleafs)
            models.add(prop.model)

        # Lock down the order of the names.
        model_list = list(models)
        model_ind = {
            mdl: i
            for i, mdl in enumerate(model_list)
        }

        game_lump = self.game_lumps[b'sprp']

        # Now write out the sections.
        prop_lump = BytesIO()
        prop_lump.write(struct.pack('<i', len(model_list)))
        for name in model_list:
            prop_lump.write(struct.pack('<128s', name.encode('ascii')))

        prop_lump.write(struct.pack('<i', len(leaf_array)))
        prop_lump.write(struct.pack('<{}H'.format(len(leaf_array)), *leaf_array))

        prop_lump.write(struct.pack('<i', len(props)))
        for leaf_off, prop in zip(leaf_offsets, props):
            prop_lump.write(struct.pack(
                '<6fH',
                prop.origin.x,
                prop.origin.y,
                prop.origin.z,
                prop.angles.pitch,
                prop.angles.yaw,
                prop.angles.roll,
                model_ind[prop.model],
            ))

            prop_lump.write(struct.pack(
                '<HHBBifffff',
                leaf_off,
                len(prop.visleafs),
                prop.solidity,
                prop.flags.value_prim,
                prop.skin,
                prop.min_fade,
                prop.max_fade,
                prop.lighting.x,
                prop.lighting.y,
                prop.lighting.z,
            ))
            if game_lump.version >= 5:
                prop_lump.write(struct.pack('<f', prop.fade_scale))

            if game_lump.version in (6, 7):
                prop_lump.write(struct.pack(
                    '<HH',
                    prop.min_dx_level,
                    prop.max_dx_level,
                ))

            if game_lump.version >= 8:
                prop_lump.write(struct.pack(
                    '<BBBB',
                    prop.min_cpu_level,
                    prop.max_cpu_level,
                    prop.min_gpu_level,
                    prop.max_gpu_level
                ))

            if game_lump.version >= 7:
                prop_lump.write(struct.pack(
                    '<BBBB',
                    int(prop.tint.x),
                    int(prop.tint.y),
                    int(prop.tint.z),
                    prop.renderfx,
                ))

            if game_lump.version >= 10:
                prop_lump.write(struct.pack('<I', prop.flags.value_sec))

            if game_lump.version >= 11:
                # Unknown padding/data, though it's always zero.

                prop_lump.write(struct.pack('<xxxxf', prop.scaling))
            elif game_lump.version >= 9:
                # The 1-byte bool gets expanded to the full 4-byte size.
                prop_lump.write(struct.pack('<?xxx', prop.disable_on_xbox))

        game_lump.data = prop_lump.getvalue()

    def vis_tree(self) -> 'VisTree':
        """Parse the visleaf data to get the full node."""
        # First parse everything, then link up the objects.
        planes: List[Tuple[Vec, float]] = []
        nodes: List[Tuple[VisTree, int, int]] = []
        leafs: List[VisLeaf] = []

        for x, y, z, dist, flags in struct.iter_unpack('<ffffi', self.lumps[BSP_LUMPS.PLANES].data):
            planes.append((Vec(x, y, z), dist))

        for (
            plane_ind, neg_ind, pos_ind,
            min_x, min_y, min_z,
            max_x, max_y, max_z,
            first_face, face_count, area_ind,
        ) in struct.iter_unpack('<iii6hHHh2x', self.lumps[BSP_LUMPS.NODES].data):
            plane_norm, plane_dist = planes[plane_ind]
            nodes.append((VisTree(
                plane_norm, plane_dist,
                Vec(min_x, min_y, min_z),
                Vec(max_x, max_y, max_z),
            ), neg_ind, pos_ind))

        leaf_fmt = '<ihh6h4Hh2x'
        # Some extra ambient light data.
        if self.version.value <= 19:
            leaf_fmt += '26x'

        for i, (
            contents,
            cluster_ind, area_and_flags,
            min_x, min_y, min_z,
            max_x, max_y, max_z,
            first_face, num_faces,
            first_brush, num_brushes,
            water_ind
        ) in enumerate(struct.iter_unpack(leaf_fmt, self.lumps[BSP_LUMPS.LEAFS].data)):
            area = area_and_flags >> 7
            flags = area_and_flags & 0b1111111
            leafs.append(VisLeaf(
                i, area, flags,
                Vec(min_x, min_y, min_z),
                Vec(max_x, max_y, max_z),
                first_face, num_faces,
                first_brush, num_brushes,
                water_ind,
            ))

        for node, neg_ind, pos_ind in nodes:
            if neg_ind < 0:
                node.child_neg = leafs[-1 - neg_ind]
            else:
                node.child_neg = nodes[neg_ind][0]
            if pos_ind < 0:
                node.child_pos = leafs[-1 - pos_ind]
            else:
                node.child_pos = nodes[pos_ind][0]
        # First node is the top of the tree.
        return nodes[0][0]


@attr.define(eq=False)
class Lump:
    """Represents a lump header in a BSP file.

    """
    type: BSP_LUMPS
    version: int
    ident: bytes
    data: bytes = b''

    def __repr__(self) -> str:
        return '<BSP Lump {!r}, v{}, ident={!r}, {} bytes>'.format(
            self.type.name,
            self.version,
            self.ident,
            len(self.data),
        )


@attr.define(eq=False)
class GameLump:
    """Represents a game lump.

    These are designed to be game-specific.
    """
    id: bytes
    flags: int
    version: int
    data: bytes = b''

    ST: ClassVar[struct.Struct] = struct.Struct('<4s HH ii')

    def __repr__(self) -> str:
        return '<GameLump {}, flags={}, v{}, {} bytes>'.format(
            repr(self.id)[1:],
            self.flags,
            self.version,
            len(self.data),
        )


@attr.define(eq=True)
class TexData:
    """Represents some additional infomation for textures."""
    mat: str
    reflectivity: Vec
    width: int
    height: int
    # TexData has a 'view' width/height too, but it isn't used at all and is
    # always the same as regular width/height.


@attr.define(eq=True)
class TexInfo:
    """Represents texture positioning / scaling info."""
    s_off: Vec
    s_shift: float
    t_off: Vec
    t_shift: float
    lightmap_s_off: Vec
    lightmap_s_shift: float
    lightmap_t_off: Vec
    lightmap_t_shift: float
    flags: SurfFlags
    _info: TexData

    @property
    def mat(self) -> str:
        """The material used for this texinfo."""
        return self._info.mat

    @property
    def reflectivity(self) -> Vec:
        """The reflectivity of the texture."""
        return self._info.reflectivity.copy()

    @property
    def tex_size(self) -> Tuple[int, int]:
        """The size of the texture."""
        return self._info.width, self._info.height


@attr.define(eq=False)
class StaticProp:
    """Represents a prop_static in the BSP.

    Different features were added in different versions.
    v5+ allows fade_scale.
    v6 and v7 allow min/max DXLevel.
    v8+ allows min/max GPU and CPU levels.
    v7+ allows model tinting, and renderfx.
    v9+ allows disabling on XBox 360.
    v10+ adds 4 unknown bytes (float?), and an expanded flags section.
    v11+ adds uniform scaling and removes XBox disabling.
    """
    model: str
    origin: Vec
    angles: Angle = attr.ib(factory=Angle)
    scaling: float = 1.0
    visleafs: List[int] = attr.ib(factory=list)
    solidity: int = 6
    flags: StaticPropFlags = StaticPropFlags.NONE
    skin: int = 0
    min_fade: float = 0.0
    max_fade: float = 0.0
    # If not provided, uses origin.
    lighting: Vec = attr.ib(default=attr.Factory(lambda prp: prp.origin.copy(), takes_self=True))
    fade_scale: float = -1.0
    min_dx_level: int = 0
    max_dx_level: int = 0
    min_cpu_level: int = 0
    max_cpu_level: int = 0
    min_gpu_level: int = 0
    max_gpu_level: int = 0
    tint: Vec = attr.ib(factory=lambda: Vec(255, 255, 255))
    renderfx: int = 255
    disable_on_xbox: bool = False

    def __repr__(self) -> str:
        return '<Prop "{}#{}" @ {} rot {}>'.format(
            self.model,
            self.skin,
            self.origin,
            self.angles,
        )


@attr.define(eq=False)
class Cubemap:
    """A env_cubemap positioned in the map.

    The position is integral, and the size can be zero for the default
    or a positive number for different powers of 2.
    """
    origin: Vec  # Always integer coordinates
    size: int = 0

    @property
    def resolution(self) -> int:
        """Return the actual image size."""
        if self.size == 0:
            return 32
        return 2**(self.size-1)


@attr.define(eq=False)
class Overlay:
    """An overlay embedded in the map."""
    id: int = attr.ib(eq=True)
    origin: Vec
    normal: Vec
    texture: TexInfo
    face_count: int
    faces: List[int] = attr.ib(factory=list, validator=attr.validators.deep_iterable(
        attr.validators.instance_of(int),
        attr.validators.instance_of(list),
    ))
    render_order: int = attr.ib(default=0, validator=attr.validators.in_(range(3)))
    u_min: float = 0.0
    u_max: float = 1.0
    v_min: float = 0.0
    v_max: float = 1.0
    # Four corner handles of the overlay.
    uv1: Vec = attr.ib(factory=lambda: Vec(-16, -16))
    uv2: Vec = attr.ib(factory=lambda: Vec(-16, +16))
    uv3: Vec = attr.ib(factory=lambda: Vec(+16, +16))
    uv4: Vec = attr.ib(factory=lambda: Vec(+16, -16))


@attr.define
class VisLeaf:
    """A leaf in the visleaf data.

    The bounds is defined implicitly by the parent node planes.
    """
    id: int
    area: int
    flags: int
    mins: Vec
    maxes: Vec
    first_face: int
    face_count: int
    first_brush: int
    brush_count: int
    water_id: int


@attr.define
class VisTree:
    """A tree node in the visleaf data.

    Each of these is a plane splitting the map in two, which then has a child
    tree or visleaf on either side.
    """
    plane_norm: Vec
    plane_dist: float
    mins: Vec
    maxes: Vec
    # Initialised empty, set after.
    child_neg: Union['VisTree', VisLeaf] = attr.ib(default=None)
    child_pos: Union['VisTree', VisLeaf] = attr.ib(default=None)
