#
# This file is part of LiteX.
#
# This file is Copyright (c) 2013-2014 Sebastien Bourdeauducq <sb@m-labs.hk>
# This file is Copyright (c) 2014-2024 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2018 Dolu1990 <charles.papon.90@gmail.com>
# This file is Copyright (c) 2019 Gabriel L. Somlo <gsomlo@gmail.com>
# This file is Copyright (c) 2018 Jean-François Nguyen <jf@lambdaconcept.fr>
# This file is Copyright (c) 2019 Antmicro <www.antmicro.com>
# This file is Copyright (c) 2013 Robert Jordens <jordens@gmail.com>
# This file is Copyright (c) 2018 Sean Cross <sean@xobs.io>
# This file is Copyright (c) 2018 Sergiusz Bazanski <q3k@q3k.org>
# This file is Copyright (c) 2018-2016 Tim 'mithro' Ansell <me@mith.ro>
# This file is Copyright (c) 2015 whitequark <whitequark@whitequark.org>
# This file is Copyright (c) 2018 William D. Jones <thor0505@comcast.net>
# This file is Copyright (c) 2020 Piotr Esden-Tempski <piotr@esden.net>
# This file is Copyright (c) 2022 Franck Jullien <franck.jullien@collshade.fr>
# SPDX-License-Identifier: BSD-2-Clause

import os
import re
import json
import time
import datetime
import inspect
from shutil import which
from sysconfig import get_platform

from migen import *

from litex.soc.interconnect.csr import CSRStatus
from litex.soc.integration.soc import SoCRegion

from litex.build.tools import generated_separator, generated_banner

from litex.soc.doc.rst import reflow
from litex.soc.doc.module import gather_submodules, ModuleNotDocumented, DocumentedModule, DocumentedInterrupts
from litex.soc.doc.csr import DocumentedCSRRegion
from litex.soc.interconnect.csr import _CompoundCSR

# CPU files ----------------------------------------------------------------------------------------

def get_cpu_mak(cpu, compile_software):
    # Select between CLANG and GCC.
    clang = os.getenv("CLANG", "")
    if clang != "":
        clang = bool(int(clang))
    else:
        clang = None
    if cpu.clang_triple is None:
        if clang:
            raise ValueError(cpu.name + " is not supported with CLANG.")
        else:
            clang = False
    else:
        # Default to gcc unless told otherwise.
        if clang is None:
            clang = False
    assert isinstance(clang, bool)
    if clang:
        triple = cpu.clang_triple
        flags = cpu.clang_flags
    else:
        triple = cpu.gcc_triple
        flags = cpu.gcc_flags

    # Select triple when more than one.
    def select_triple(triple):
        r = None
        if not isinstance(triple, tuple):
            triple = (triple,)
        override = os.getenv("LITEX_ENV_CC_TRIPLE")
        if override:
            triple = (override,) + triple
        p = get_platform()
        for i in range(len(triple)):
            t = triple[i]
            # Use native toolchain if host and target platforms are the same.
            if t == "riscv64-unknown-elf" and p == "linux-riscv64":
                r = '--native--'
                break
            if which(t+"-gcc"):
                r = t
                break
        if r is None:
            if not compile_software:
                return "--not-found--"
            msg = "Unable to find any of the cross compilation toolchains:\n"
            for i in range(len(triple)):
                msg += "- " + triple[i] + "\n"
            raise OSError(msg)
        return r
    selected_triple = select_triple(triple)

    # RISC-V's march zicsr workaround (for binutils >= 2.37).
    def get_binutils_version():
        version = 0
        for i, l in enumerate(os.popen(selected_triple + "-ar -V")):
            # Version is last float reported in first line.
            if i == 0:
                version = float(re.findall(r"\d+\.\d+", l)[-1])
        return version

    def apply_riscv_zicsr_march_workaround(flags):
        # Append _zicsr to march when binutils >= 2.37 and zicsr is not present.
        if (get_binutils_version() >= 2.37) and ("zicsr" not in flags):
            flags = re.compile("-march=([^ ]+)").sub("-march=\\1_zicsr", flags)
        return flags

    #if (not clang) and ("riscv" in selected_triple):
    #   flags = apply_riscv_zicsr_march_workaround(flags)

    # Return informations.
    return [
        ("TRIPLE",        selected_triple),
        ("CPU",           cpu.name),
        ("CPUFAMILY",     cpu.family),
        ("CPUFLAGS",      flags),
        ("CPUENDIANNESS", cpu.endianness),
        ("CLANG",         str(int(clang))),
        ("CPU_DIRECTORY", os.path.dirname(inspect.getfile(cpu.__class__))),
    ]


def get_linker_output_format(cpu):
    return f"OUTPUT_FORMAT(\"{cpu.linker_output_format}\")\n"


def get_linker_regions(regions):
    r = "MEMORY {\n"
    for name, region in regions.items():
        r += f"\t{name} : ORIGIN = 0x{region.origin:08x}, LENGTH = 0x{region.size:08x}\n"
    r += "}\n"
    return r


# C Export -----------------------------------------------------------------------------------------


# Header.

def get_git_header():
    from litex.build.tools import get_litex_git_revision
    r = generated_banner("//")
    r += "#ifndef __GENERATED_GIT_H\n#define __GENERATED_GIT_H\n\n"
    r += f"#define LITEX_GIT_SHA1 \"{get_litex_git_revision()}\"\n"
    r += "#endif\n"
    return r

def get_mem_header(regions):
    r = generated_banner("//")
    r += "#ifndef __GENERATED_MEM_H\n#define __GENERATED_MEM_H\n\n"
    for name, region in regions.items():
        r += f"#ifndef {name.upper()}_BASE\n"
        r += f"#define {name.upper()}_BASE 0x{region.origin:08x}L\n"
        r += f"#define {name.upper()}_SIZE 0x{region.size:08x}\n"
        r += "#endif\n\n"

    r += "#ifndef MEM_REGIONS\n"
    r += "#define MEM_REGIONS \"";
    name_length = max([len(name) for name in regions.keys()])
    for name, region in regions.items():
        r += f"{name.upper()} {' '*(name_length-len(name))} 0x{region.origin:08x} 0x{region.size:x} \\n"
    r = r[:-2]
    r += "\"\n"
    r += "#endif\n"

    r += "#endif\n"
    return r

def get_soc_header(constants, with_access_functions=True):
    r = generated_banner("//")
    r += "#ifndef __GENERATED_SOC_H\n#define __GENERATED_SOC_H\n"
    funcs = ""

    for name, value in constants.items():
        if value is None:
            r += "#define "+name+"\n"
            continue
        if isinstance(value, str):
            value = "\"" + value + "\""
            ctype = "const char *"
        else:
            value = str(value)
            ctype = "int"
        r += "#define "+name+" "+value+"\n"
        if with_access_functions:
            funcs += "static inline "+ctype+" "+name.lower()+"_read(void) {\n"
            funcs += "\treturn "+value+";\n}\n"

    if with_access_functions:
        r += "\n#ifndef __ASSEMBLER__\n"
        r += funcs
        r += "#endif // !__ASSEMBLER__\n"

    r += "\n#endif\n"
    return r

def _generate_csr_header_includes_c(with_access_functions):
    includes = ""
    if with_access_functions:
        includes += "#include <generated/soc.h>\n"
    includes += "#ifndef __GENERATED_CSR_H\n"
    includes += "#define __GENERATED_CSR_H\n"
    if with_access_functions:
        includes += "#include <stdint.h>\n"
        includes += "#include <system.h>\n"
        includes += "#ifndef CSR_ACCESSORS_DEFINED\n"
        includes += "#include <hw/common.h>\n"
        includes += "#endif /* ! CSR_ACCESSORS_DEFINED */\n"
    return includes

def _generate_csr_base_define_c(csr_base, with_csr_base_define):
    includes = ""
    if with_csr_base_define:
        includes += "\n"
        includes += "#ifndef CSR_BASE\n"
        includes += f"#define CSR_BASE {hex(csr_base)}L\n"
        includes += "#endif /* ! CSR_BASE */\n"
    return includes

# CSR Definitions.

def _get_csr_addr(csr_base, addr, with_csr_base_define=True):
    if with_csr_base_define:
        return f"(CSR_BASE + {hex(addr)}L)"
    else:
        return f"{hex(csr_base + addr)}L"

def _generate_csr_definitions_c(reg_name, reg_base, nwords, csr_base, with_csr_base_define):
    addr_str    = f"CSR_{reg_name.upper()}_ADDR"
    size_str    = f"CSR_{reg_name.upper()}_SIZE"
    definitions = f"#define {addr_str} {_get_csr_addr(csr_base, reg_base, with_csr_base_define)}\n"
    definitions += f"#define {size_str} {nwords}\n"
    return definitions

def _generate_csr_region_definitions_c(name, region, origin, alignment, csr_base, with_csr_base_define):
    base_define = with_csr_base_define and not isinstance(region, MockCSRRegion)
    base = csr_base if not isinstance(region, MockCSRRegion) else 0
    region_defs = f"\n/* {name.upper()} Registers */\n"
    region_defs += f"#define CSR_{name.upper()}_BASE {_get_csr_addr(base, origin, base_define)}\n"

    if not isinstance(region.obj, Memory):
        for csr in region.obj:
            nr = (csr.size + region.busword - 1) // region.busword
            region_defs += _generate_csr_definitions_c(
                reg_name              = name + "_" + csr.name,
                reg_base              = origin,
                nwords                = nr,
                csr_base              = base,
                with_csr_base_define  = base_define,
            )
            origin += alignment // 8 * nr

    region_defs += f"\n/* {name.upper()} Fields */\n"
    if not isinstance(region.obj, Memory):
        for csr in region.obj:
            if hasattr(csr, "fields"):
                region_defs += _generate_csr_field_definitions_c(csr, name)

    return region_defs

# CSR Read/Write Access Functions.

def _determine_ctype_and_stride_c(size, alignment):
    if size > 8:
        return None, None
    elif size > 4:
        ctype = "uint64_t"
    elif size > 2:
        ctype = "uint32_t"
    elif size > 1:
        ctype = "uint16_t"
    else:
        ctype = "uint8_t"
    stride = alignment // 8
    return ctype, stride

def _generate_csr_read_function_c(reg_name, reg_base, nwords, busword, ctype, stride, csr_base, with_csr_base_define):
    read_function = f"static inline {ctype} {reg_name}_read(void) {{\n"
    if nwords > 1:
        read_function += f"\t{ctype} r = csr_read_simple({_get_csr_addr(csr_base, reg_base, with_csr_base_define)});\n"
        for sub in range(1, nwords):
            read_function += f"\tr <<= {busword};\n"
            read_function += f"\tr |= csr_read_simple({_get_csr_addr(csr_base, reg_base + sub * stride, with_csr_base_define)});\n"
        read_function += "\treturn r;\n}\n"
    else:
        read_function += f"\treturn csr_read_simple({_get_csr_addr(csr_base, reg_base, with_csr_base_define)});\n}}\n"
    return read_function

def _generate_csr_write_function_c(reg_name, reg_base, nwords, busword, ctype, stride, csr_base, with_csr_base_define):
    write_function = f"static inline void {reg_name}_write({ctype} v) {{\n"
    for sub in range(nwords):
        shift = (nwords - sub - 1) * busword
        v_shift = f"v >> {shift}" if shift else "v"
        write_function += f"\tcsr_write_simple({v_shift}, {_get_csr_addr(csr_base, reg_base + sub * stride, with_csr_base_define)});\n"
    write_function += "}\n"
    return write_function

def _get_csr_read_write_access_functions_c(reg_name, reg_base, nwords, busword, alignment, read_only, csr_base, with_csr_base_define):
    result = ""
    size   = nwords * busword // 8

    ctype, stride = _determine_ctype_and_stride_c(size, alignment)
    if ctype is None:
        return result

    result += _generate_csr_read_function_c(reg_name, reg_base, nwords, busword, ctype, stride, csr_base, with_csr_base_define)
    if not read_only:
        result += _generate_csr_write_function_c(reg_name, reg_base, nwords, busword, ctype, stride, csr_base, with_csr_base_define)

    return result

def _generate_csr_region_access_functions_c(name, region, origin, alignment, csr_base, with_csr_base_define):
    base_define = with_csr_base_define and not isinstance(region, MockCSRRegion)
    region_defs = f"\n/* {name.upper()} Access Functions */\n"

    if not isinstance(region.obj, Memory):
        for csr in region.obj:
            nr = (csr.size + region.busword - 1) // region.busword
            region_defs += _get_csr_read_write_access_functions_c(
                reg_name              = name + "_" + csr.name,
                reg_base              = origin,
                nwords                = nr,
                busword               = region.busword,
                alignment             = alignment,
                read_only             = getattr(csr, "read_only", False),
                csr_base              = csr_base,
                with_csr_base_define  = base_define,
            )
            origin += alignment // 8 * nr
    return region_defs

# CSR Fields.

def _generate_csr_field_definitions_c(csr, name):
    field_defs = ""
    for field in csr.fields.fields:
        offset = str(field.offset)
        size   = str(field.size)
        field_defs += f"#define CSR_{name.upper()}_{csr.name.upper()}_{field.name.upper()}_OFFSET {offset}\n"
        field_defs += f"#define CSR_{name.upper()}_{csr.name.upper()}_{field.name.upper()}_SIZE {size}\n"
    return field_defs

def _generate_csr_field_accessors_c(name, csr, field):
    accessors = ""
    if csr.size <= 32:
        reg_name   = name + "_" + csr.name.lower()
        field_name = reg_name + "_" + field.name.lower()
        offset     = str(field.offset)
        size       = str(field.size)
        accessors += f"static inline uint32_t {field_name}_extract(uint32_t oldword) {{\n"
        accessors += f"\tuint32_t mask = 0x{(1 << int(size)) - 1:x};\n"
        accessors += f"\treturn ((oldword >> {offset}) & mask);\n}}\n"
        accessors += f"static inline uint32_t {field_name}_read(void) {{\n"
        accessors += f"\tuint32_t word = {reg_name}_read();\n"
        accessors += f"\treturn {field_name}_extract(word);\n}}\n"
        if not getattr(csr, "read_only", False):
            accessors += f"static inline uint32_t {field_name}_replace(uint32_t oldword, uint32_t plain_value) {{\n"
            accessors += f"\tuint32_t mask = 0x{(1 << int(size)) - 1:x};\n"
            accessors += f"\treturn (oldword & (~(mask << {offset}))) | ((mask & plain_value) << {offset});\n}}\n"
            accessors += f"static inline void {field_name}_write(uint32_t plain_value) {{\n"
            accessors += f"\tuint32_t oldword = {reg_name}_read();\n"
            accessors += f"\tuint32_t newword = {field_name}_replace(oldword, plain_value);\n"
            accessors += f"\t{reg_name}_write(newword);\n}}\n"
    return accessors

def _generate_csr_field_functions_c(csr, name):
    field_funcs = ""
    for field in csr.fields.fields:
            field_funcs += _generate_csr_field_accessors_c(name, csr, field)
    return field_funcs

def _generate_csr_fields_access_functions_c(name, region, origin, alignment, csr_base, with_csr_base_define):
    base_define = with_csr_base_define and not isinstance(region, MockCSRRegion)
    region_defs = f"\n/* {name.upper()} Fields Access Functions */\n"

    if not isinstance(region.obj, Memory):
        for csr in region.obj:
            nr = (csr.size + region.busword - 1) // region.busword
            origin += alignment // 8 * nr
            if hasattr(csr, "fields"):
                region_defs += _generate_csr_field_functions_c(csr, name)
    return region_defs

# CSR Header.

def get_csr_header(regions, constants, csr_base=None, with_csr_base_define=True, with_access_functions=True, with_fields_access_functions=False):
    """
    Generate the CSR header file content.
    """

    alignment = constants.get("CONFIG_CSR_ALIGNMENT", 32)
    r = generated_banner("//")

    # CSR Includes.
    r += "\n"
    r += generated_separator("//", "CSR Includes.")
    r += "\n"
    r += _generate_csr_header_includes_c(with_access_functions)
    _csr_base = regions[next(iter(regions))].origin
    csr_base  = csr_base if csr_base is not None else _csr_base
    r += _generate_csr_base_define_c(csr_base, with_csr_base_define)

    # CSR Registers/Fields Definition.
    r += "\n"
    r += generated_separator("//", "CSR Registers/Fields Definition.")
    for name, region in regions.items():
        origin = region.origin - _csr_base
        r += _generate_csr_region_definitions_c(name, region, origin, alignment, csr_base, with_csr_base_define)

    # CSR Registers Access Functions.
    if with_access_functions:
        r += "\n"
        r += generated_separator("//", "CSR Registers Access Functions.")
        r += "\n"
        r += "#ifndef LITEX_CSR_ACCESS_FUNCTIONS\n"
        r += "#define LITEX_CSR_ACCESS_FUNCTIONS 1\n"
        r += "#endif\n"
        r += "\n"
        r += "#if LITEX_CSR_ACCESS_FUNCTIONS\n"
        for name, region in regions.items():
            origin = region.origin - _csr_base
            r += _generate_csr_region_access_functions_c(name, region, origin, alignment, csr_base, with_csr_base_define)
        r += "#endif /* LITEX_CSR_ACCESS_FUNCTIONS */\n"

    # CSR Registers Field Access Functions.
    if with_fields_access_functions:
        r += "\n"
        r += generated_separator("//", "CSR Registers Field Access Functions.")
        r += "\n"
        r += "#ifndef LITEX_CSR_FIELDS_ACCESS_FUNCTIONS\n"
        r += "#define LITEX_CSR_FIELDS_ACCESS_FUNCTIONS 1\n"
        r += "#endif\n"
        r += "\n"
        r += "#if LITEX_CSR_FIELDS_ACCESS_FUNCTIONS\n"
        for name, region in regions.items():
            origin = region.origin - _csr_base
            r += _generate_csr_fields_access_functions_c(name, region, origin, alignment, csr_base, with_csr_base_define)
        r += "#endif /* LITEX_CSR_FIELDS_ACCESS_FUNCTIONS */\n"

    r += "\n#endif /* ! __GENERATED_CSR_H */\n"
    return r

# C I2C Export -------------------------------------------------------------------------------------

def get_i2c_header(i2c_init_values):
    i2c_devs, i2c_init = i2c_init_values

    r = generated_banner("//")
    r += "#ifndef __GENERATED_I2C_H\n#define __GENERATED_I2C_H\n\n"
    r += "#include <libbase/i2c.h>\n\n"
    r += "#define I2C_DEVS_COUNT {}\n\n".format(len(i2c_devs))

    devs = {}
    default_dev = 0
    r += "struct i2c_dev i2c_devs[{}] = {{\n".format(len(i2c_devs))
    for i, (name, is_default) in enumerate(sorted(i2c_devs)):
        devs[name] = i
        if is_default:
            default_dev = i
        r += "\t{\n"
        r += "\t\t.ops.write          = {}_w_write,\n".format(name)
        r += "\t\t.ops.read           = {}_r_read,\n".format(name)
        r += "\t\t.ops.w_scl_offset   = CSR_{}_W_SCL_OFFSET,\n".format(name.upper())
        r += "\t\t.ops.w_sda_offset   = CSR_{}_W_SDA_OFFSET,\n".format(name.upper())
        r += "\t\t.ops.w_oe_offset    = CSR_{}_W_OE_OFFSET,\n".format(name.upper())
        r += "\t\t.name               = \"{}\"\n".format(name)
        r += "\t},\n"
    r += "};\n\n"

    r += "#define DEFAULT_I2C_DEV {}\n\n".format(default_dev)

    if i2c_init:
        r += "struct i2c_cmds {\n"
        r += "\tint dev;\n"
        r += "\tuint32_t *init_table;\n"
        r += "\tint nb_cmds;\n"
        r += "\tint addr_len;\n"
        r += "\tint i2c_addr;\n"
        r += "};\n"

        r += "\n#define I2C_INIT\n"
        r += "#define I2C_INIT_CNT {}\n\n".format(len(i2c_init))

        for i, (name, i2c_addr, table, _) in enumerate(i2c_init):
            r += "uint32_t {}_{}_{}_init_table[{}] = {{\n".format(name, hex(i2c_addr), i, len(table) * 2)
            for addr, data in table:
                r += "\t0x{:04X}, 0x{:02X},\n".format(addr, data)
            r += "};\n"

        r += "static struct i2c_cmds i2c_init[I2C_INIT_CNT] = {\n"
        for i, (name, i2c_addr, table, addr_len) in enumerate(i2c_init):
            r += "\t{\n"
            r += "\t\t.dev        = {},\n".format(devs[name])
            r += "\t\t.init_table = {}_{}_{}_init_table,\n".format(name, hex(i2c_addr), i)
            r += "\t\t.nb_cmds    = {},\n".format(len(table))
            r += "\t\t.i2c_addr   = {},\n".format(hex(i2c_addr))
            r += "\t\t.addr_len   = {},\n".format(addr_len)
            r += "\t},\n"
        r += "};\n"

    r += "\n#endif\n"
    return r

# JSON Export / Import  ----------------------------------------------------------------------------

def get_csr_json(csr_regions={}, constants={}, mem_regions={}):
    alignment = constants.get("CONFIG_CSR_ALIGNMENT", 32)

    d = {
        "csr_bases":     {},
        "csr_registers": {},
        "constants":     {},
        "memories":      {},
    }

    # Get CSR Regions.
    for name, region in csr_regions.items():
        d["csr_bases"][name] = region.origin
        region_origin = region.origin
        if not isinstance(region.obj, Memory):
            for csr in region.obj:
                _size = (csr.size + region.busword - 1)//region.busword
                _type = "rw"
                if isinstance(csr, CSRStatus) and not hasattr(csr, "r"):
                    _type = "ro"
                d["csr_registers"][name + "_" + csr.name] = {
                    "addr": region_origin,
                    "size": _size,
                    "type": _type
                }
                region_origin += alignment//8*_size

    # Get Constants.
    for name, value in constants.items():
        d["constants"][name.lower()] = value.lower() if isinstance(value, str) else value

    # Get Mem Regions.
    for name, region in mem_regions.items():
        d["memories"][name.lower()] = {
            "base": region.origin,
            "size": region.size,
            "type": region.type,
        }

    # Return JSON Dump.
    return json.dumps(d, indent=4)

class MockCSR:
    def __init__(self, name, size, type):
        self.name = name
        self.size = size
        self.type = type

class MockCSRRegion:
    def __init__(self, origin, obj):
        self.origin  = origin
        self.obj     = obj
        self.busword = 32

def load_csr_json(filename, origin=0, name=""):
    if len(name):
        name += "_"
    # Read File.
    with open(filename, 'r') as json_file:
        config_data = json.load(json_file)

    # Load CSR Regions.
    csr_regions = {}
    for region_name, addr in config_data.get("csr_bases", {}).items():
        csrs = []
        for csr_name, info in config_data.get("csr_registers", {}).items():
            region_prefix, _, csr_suffix = csr_name.rpartition("_")
            if region_prefix.startswith(region_name):
                if region_prefix == region_name:
                    final_name = csr_suffix
                else:
                    final_name = f"{region_prefix[len(region_name) + 1:]}_{csr_suffix}"
                csrs.append(MockCSR(final_name, info["size"], info["type"]))
        csr_regions[name + region_name] = MockCSRRegion(origin + addr, csrs)

    # Load Constants.
    constants = {(name + const_name).upper(): value for const_name, value in config_data.get("constants", {}).items()}

    # Load Memory Regions.
    mem_regions = {}
    for mem_name, info in config_data.get("memories", {}).items():
        mem_regions[name + mem_name.lower()] = SoCRegion(origin + info["base"], info["size"], info["type"])

    # Return CSR Regions, Constants, Mem Regions.
    return csr_regions, constants, mem_regions

# CSV Export --------------------------------------------------------------------------------------

def get_csr_csv(csr_regions={}, constants={}, mem_regions={}):
    d = json.loads(get_csr_json(csr_regions, constants, mem_regions))
    r = generated_banner("#")
    for name, value in d["csr_bases"].items():
        r += "csr_base,{},0x{:08x},,\n".format(name, value)
    for name in d["csr_registers"].keys():
        r += "csr_register,{},0x{:08x},{},{}\n".format(name,
            d["csr_registers"][name]["addr"],
            d["csr_registers"][name]["size"],
            d["csr_registers"][name]["type"])
    for name, value in d["constants"].items():
        r += "constant,{},{},,\n".format(name, value)
    for name in d["memories"].keys():
        r += "memory_region,{},0x{:08x},{:d},{:s}\n".format(name,
            d["memories"][name]["base"],
            d["memories"][name]["size"],
            d["memories"][name]["type"],
            )
    return r

# SVD Export --------------------------------------------------------------------------------------

def get_csr_svd(soc, vendor="litex", name="soc", description=None):
    def sub_csr_bit_range(busword, csr, offset):
        nwords = (csr.size + busword - 1)//busword
        i = nwords - offset - 1
        nbits = min(csr.size - i*busword, busword) - 1
        name = (csr.name + str(i) if nwords > 1 else csr.name).upper()
        origin = i*busword
        return (origin, nbits, name)

    def print_svd_register(csr, csr_address, description, length, svd):
        svd.append('                <register>')
        svd.append('                    <name>{}</name>'.format(csr.short_numbered_name))
        if description is not None:
            svd.append('                    <description><![CDATA[{}]]></description>'.format(description))
        svd.append('                    <addressOffset>0x{:04x}</addressOffset>'.format(csr_address))
        svd.append('                    <resetValue>0x{:02x}</resetValue>'.format(csr.reset_value))
        svd.append('                    <size>{}</size>'.format(length))
        # svd.append('                    <access>{}</access>'.format(csr.access))  # 'access' is a lie: "read-only" registers can legitimately change state based on a write, and is in fact used to handle the "pending" field in events
        csr_address = csr_address + 4
        svd.append('                    <fields>')
        if hasattr(csr, "fields") and len(csr.fields) > 0:
            for field in csr.fields:
                svd.append('                        <field>')
                svd.append('                            <name>{}</name>'.format(field.name))
                svd.append('                            <msb>{}</msb>'.format(field.offset +
                                                                         field.size - 1))
                svd.append('                            <bitRange>[{}:{}]</bitRange>'.format(
                    field.offset + field.size - 1, field.offset))
                svd.append('                            <lsb>{}</lsb>'.format(field.offset))
                svd.append('                            <description><![CDATA[{}]]></description>'.format(
                    reflow(field.description)))
                svd.append('                        </field>')
        else:
            field_size = csr.size
            field_name = csr.short_name.lower()
            # Strip off "ev_" from eventmanager fields
            if field_name == "ev_enable":
                field_name = "enable"
            elif field_name == "ev_pending":
                field_name = "pending"
            elif field_name == "ev_status":
                field_name = "status"
            svd.append('                        <field>')
            svd.append('                            <name>{}</name>'.format(field_name))
            svd.append('                            <msb>{}</msb>'.format(field_size - 1))
            svd.append('                            <bitRange>[{}:{}]</bitRange>'.format(field_size - 1, 0))
            svd.append('                            <lsb>{}</lsb>'.format(0))
            svd.append('                        </field>')
        svd.append('                    </fields>')
        svd.append('                </register>')

    interrupts = {}
    for csr, irq in sorted(soc.irq.locs.items()):
        interrupts[csr] = irq

    documented_regions = []
    for region_name, region in soc.csr.regions.items():
        documented_regions.append(DocumentedCSRRegion(
            name           = region_name,
            region         = region,
            csr_data_width = soc.csr.data_width)
        )

    svd = []
    svd.append('<?xml version="1.0" encoding="utf-8"?>')
    svd.append('')
    svd.append('<device schemaVersion="1.1" xmlns:xs="http://www.w3.org/2001/XMLSchema-instance" xs:noNamespaceSchemaLocation="CMSIS-SVD.xsd" >')
    svd.append('    <vendor>{}</vendor>'.format(vendor))
    svd.append('    <name>{}</name>'.format(name.upper()))
    if description is not None:
        svd.append('    <description><![CDATA[{}]]></description>'.format(reflow(description)))
    else:
        fmt = "%Y-%m-%d %H:%M:%S"
        build_time = datetime.datetime.fromtimestamp(time.time()).strftime(fmt)
        svd.append('    <description><![CDATA[{}]]></description>'.format(reflow("Litex SoC " + build_time)))
    svd.append('')
    svd.append('    <addressUnitBits>8</addressUnitBits>')
    svd.append('    <width>32</width>')
    svd.append('    <size>32</size>')
    svd.append('    <access>read-write</access>')
    svd.append('    <resetValue>0x00000000</resetValue>')
    svd.append('    <resetMask>0xFFFFFFFF</resetMask>')
    svd.append('')
    svd.append('    <peripherals>')

    for region in documented_regions:
        csr_address = 0
        svd.append('        <peripheral>')
        svd.append('            <name>{}</name>'.format(region.name.upper()))
        svd.append('            <baseAddress>0x{:08X}</baseAddress>'.format(region.origin))
        svd.append('            <groupName>{}</groupName>'.format(region.name.upper()))
        if len(region.sections) > 0:
            svd.append('            <description><![CDATA[{}]]></description>'.format(
                reflow(region.sections[0].body())))
        svd.append('            <registers>')
        for csr in region.csrs:
            description = None
            if hasattr(csr, "description"):
                description = csr.description
            if isinstance(csr, _CompoundCSR) and len(csr.simple_csrs) > 1:
                is_first = True
                for i in range(len(csr.simple_csrs)):
                    (start, length, name) = sub_csr_bit_range(
                        region.busword, csr, i)
                    if length > 0:
                        bits_str = "Bits {}-{} of `{}`.".format(
                            start, start+length, csr.name)
                    else:
                        bits_str = "Bit {} of `{}`.".format(
                            start, csr.name)
                    if is_first:
                        if description is not None:
                            print_svd_register(
                                csr.simple_csrs[i], csr_address, bits_str + " " + description, length, svd)
                        else:
                            print_svd_register(
                                csr.simple_csrs[i], csr_address, bits_str, length, svd)
                        is_first = False
                    else:
                        print_svd_register(
                            csr.simple_csrs[i], csr_address, bits_str, length, svd)
                    csr_address = csr_address + 4
            else:
                length = ((csr.size + region.busword - 1) //
                            region.busword) * region.busword
                print_svd_register(
                    csr, csr_address, description, length, svd)
                csr_address = csr_address + 4
        svd.append('            </registers>')
        svd.append('            <addressBlock>')
        svd.append('                <offset>0</offset>')
        svd.append('                <size>0x{:x}</size>'.format(csr_address))
        svd.append('                <usage>registers</usage>')
        svd.append('            </addressBlock>')
        if region.name in interrupts:
            svd.append('            <interrupt>')
            svd.append('                <name>{}</name>'.format(region.name))
            svd.append('                <value>{}</value>'.format(interrupts[region.name]))
            svd.append('            </interrupt>')
        svd.append('        </peripheral>')
    svd.append('    </peripherals>')
    svd.append('    <vendorExtensions>')

    if len(soc.mem_regions) > 0:
        svd.append('        <memoryRegions>')
        for region_name, region in soc.mem_regions.items():
            svd.append('            <memoryRegion>')
            svd.append('                <name>{}</name>'.format(region_name.upper()))
            svd.append('                <baseAddress>0x{:08X}</baseAddress>'.format(region.origin))
            svd.append('                <size>0x{:08X}</size>'.format(region.size))
            svd.append('            </memoryRegion>')
        svd.append('        </memoryRegions>')

    svd.append('        <constants>')
    for name, value in soc.constants.items():
        svd.append('            <constant name="{}" value="{}" />'.format(name, value))
    svd.append('        </constants>')

    svd.append('    </vendorExtensions>')
    svd.append('</device>')
    return "\n".join(svd)


# Memory.x Export ----------------------------------------------------------------------------------

def get_memory_x(soc):
    r = get_linker_regions(soc.mem_regions)
    r += '\n'
    r += 'REGION_ALIAS("REGION_TEXT", rom);\n'
    r += 'REGION_ALIAS("REGION_RODATA", rom);\n'
    r += 'REGION_ALIAS("REGION_DATA", sram);\n'
    r += 'REGION_ALIAS("REGION_BSS", sram);\n'
    r += 'REGION_ALIAS("REGION_HEAP", sram);\n'
    r += 'REGION_ALIAS("REGION_STACK", sram);\n\n'
    r += '/* CPU reset location. */\n'
    r += '_stext = {:#08x};\n'.format(soc.cpu.reset_address)
    return r
