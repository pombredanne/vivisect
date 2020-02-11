try:
    import queue
except Exception:
    import Queue as queue

import logging
import warnings
import traceback
import threading
import contextlib
import collections

import envi
import envi.bits as e_bits
import envi.memory as e_mem
import envi.codeflow as e_codeflow
import envi.pagelookup as e_page

import vstruct.cparse as vs_cparse
import vstruct.builder as vs_builder
import vstruct.constants as vs_const

import vivisect.const as v_const
import vivisect.impapi as v_impapi
import vivisect.analysis as v_analysis
import vivisect.codegraph as v_codegraph

from envi.threads import firethread

logger = logging.getLogger(__name__)

vaset_xlate = {
    int: v_const.VASET_ADDRESS,
    str: v_const.VASET_STRING,
}


'''
Mostly this is a place to scuttle away some of the inner workings
of a workspace, so the outer facing API is a little cleaner.
'''


class VivEventCore(object):
    '''
    A class to facilitate event monitoring in the viv workspace.
    '''

    def __init__(self, vw=None, **kwargs):
        self._ve_vw = vw
        self._ve_ehand = [None for x in range(v_const.VWE_MAX)]
        self._ve_thand = [None for x in range(v_const.VTE_MAX)]
        self._ve_lock = threading.Lock()

        # Find and put handler functions into the list
        for name in dir(self):
            if name.startswith('VWE_'):
                idx = getattr(v_const, name, None)
                self._ve_ehand[idx] = getattr(self, name)
            if name.startswith('VTE_'):
                idx = getattr(v_const, name, None)
                self._ve_thand[idx] = getattr(self, name)

    def _ve_fireEvent(self, event, edata):
        hlist = self._ve_ehand
        if event & v_const.VTE_MASK:
            event ^= v_const.VTE_MASK
            hlist = self._ve_thand

        h = hlist[event]
        if h is not None:
            try:
                h(self._ve_vw, event, edata)
            except Exception:
                traceback.print_exc()

    @firethread
    def _ve_fireListener(self):
        chanid = self._ve_vw.createEventChannel()
        try:
            etup = self._ve_vw.waitForEvent(chanid)
            while etup is not None:
                # TODO: WHY?!!?
                self._ve_lock.acquire()
                self._ve_lock.release()

                self._ve_fireEvent(*etup)

                etup = self._ve_vw.waitForEvent(chanid)

        finally:
            self._ve_vw.deleteEventChannel(chanid)

    # TODO: Who even uses these?
    def _ve_freezeEvents(self):
        self._ve_lock.acquire()

    def _ve_thawEvents(self):
        self._ve_lock.release()


class VivEventDist(VivEventCore):
    '''
    Similar to an event core, but does optimized distribution
    to a set of sub eventcore objects (think GUI windows...)
    '''

    def __init__(self, vw=None, **kwargs):
        if vw is None:
            raise Exception("VivEventDist requires a vw argument")

        VivEventCore.__init__(self, vw)
        self._ve_subs = [[] for x in range(v_const.VWE_MAX)]
        self._ve_tsubs = [[] for x in range(v_const.VTE_MAX)]

        self.addEventCore(self)

        # event distributors pretty much always need a thread
        self._ve_fireListener()

    def addEventCore(self, core):
        for i in range(v_const.VWE_MAX):
            h = core._ve_ehand[i]
            if h is not None:
                self._ve_subs[i].append(h)

        for i in range(v_const.VTE_MAX):
            h = core._ve_thand[i]
            if h is not None:
                self._ve_tsubs[i].append(h)

    def delEventCore(self, core):
        for i in range(v_const.VWE_MAX):
            h = core._ve_ehand[i]
            if h is not None:
                self._ve_subs[i].remove(h)

        for i in range(v_const.VTE_MAX):
            h = core._ve_thand[i]
            if h is not None:
                self._ve_tsubs[i].remove(h)

    def _ve_fireEvent(self, event, edata):
        '''
        We don't have events of our own, we just hand them down.
        '''
        subs = self._ve_subs
        if event & v_const.VTE_MASK:
            event ^= v_const.VTE_MASK
            subs = self._ve_tsubs

        hlist = subs[event]
        for h in hlist:
            try:
                h(self._ve_vw, event, edata)
            except Exception:
                traceback.print_exc()

        VivEventCore._ve_fireEvent(self, event, edata)


def ddict():
    return collections.defaultdict(dict)


class VivWorkspaceCore(v_impapi.ImportApi):

    def __init__(self):
        v_impapi.ImportApi.__init__(self)
        self.loclist = []
        self.bigend = False
        self.locmap = e_page.MapLookup()
        self.blockmap = e_page.MapLookup()
        self._mods_loaded = False

        # Storage for function local symbols
        self.localsyms = ddict()

        self._call_graph = v_codegraph.CallGraph()
        # Just in case of the GUI... :)
        self._call_graph.setMeta('bgcolor', '#000')
        self._call_graph.setMeta('nodecolor', '#00ff00')
        self._call_graph.setMeta('edgecolor', '#00802b')

        self._event_list = []
        self._event_saved = 0  # The index of the last "save" event...

        # Give ourself a structure namespace!
        self.vsbuilder = vs_builder.VStructBuilder()
        self.vsconsts = vs_const.VSConstResolver()

    def _snapInAnalysisModules(self):
        '''
        Snap in the analysis modules which are appropriate for the
        format/architecture/platform of this workspace by calling
        '''
        if self._mods_loaded:
            return

        v_analysis.addAnalysisModules(self)
        self._mods_loaded = True

    def _createSaveMark(self):
        '''
        Update the index of the most recent saved event to the current
        length of the event list (called after successful save)..
        '''
        self._event_saved = len(self._event_list)

    @contextlib.contextmanager
    def getAdminRights(self):
        self._supervisor = True
        yield
        self._supervisor = False

    def _handleADDLOCATION(self, loc):
        lva, lsize, ltype, linfo = loc
        self.locmap.setMapLookup(lva, lsize, loc)
        self.loclist.append(loc)

        # A few special handling cases...
        if ltype == v_const.LOC_IMPORT:
            # Check if the import is registered in NoReturnApis
            if self.getMeta('NoReturnApis', {}).get(linfo.lower()):
                self.cfctx.addNoReturnAddr(lva)

    def _handleDELLOCATION(self, loc):
        # FIXME delete xrefs
        lva, lsize, ltype, linfo = loc
        self.locmap.setMapLookup(lva, lsize, None)
        self.loclist.remove(loc)

    def _handleADDSEGMENT(self, einfo):
        self.segments.append(einfo)

    def _handleADDRELOC(self, einfo):
        # TODO: Whelp. 2/13/2020 is a week away. What now?
        if len(einfo) == 2:     # FIXME: legacy: remove after 02/13/2020.
            rva, rtype = einfo
            # FIXME: getFileByVa does not obey file defs
            mmva, mmsz, mmperm, fname = self.getMemoryMap(rva)
            imgbase = self.getFileMeta(fname, 'imagebase')
            data = None
            einfo = fname, rva-imgbase, rtype, data
        else:
            fname, ptroff, rtype, data = einfo
            imgbase = self.getFileMeta(fname, 'imagebase')
            rva = imgbase + ptroff

        self.reloc_by_va[rva] = rtype
        self.relocations.append(einfo)

        # RTYPE_BASERELOC assumes the memory is already accurate (eg. PE's unless rebased)

        if rtype in v_const.REBASE_TYPES:
            # add imgbase and offset to pointer in memory
            # 'data' arg must be 'offset' number
            ptr = imgbase + data
            if ptr != (ptr & e_bits.u_maxes[self.psize]):
                logger.warn(
                    'RTYPE_BASEOFF calculated a bad pointer: 0x%x (imgbase: 0x%x)', ptr, imgbase)

            # writes are costly, especially on larger binaries
            if ptr != self.readMemoryPtr(rva):
                with self.getAdminRights():
                    self.writeMemoryPtr(rva, ptr)

            logger.info('_handleADDRELOC: %x -> %x (map: 0x%x)',
                        rva, ptr, imgbase)

        if rtype == v_const.RTYPE_BASEPTR:
            # make it like a pointer (but one that could move with each load)
            #   self.addXref(va, tova, REF_PTR)
            #   ploc = self.addLocation(va, psize, LOC_POINTER)
            #   don't follow.  handle it later, once "known code" is analyzed
            self._handleADDXREF((rva, ptr, v_const.REF_PTR, 0))
            self._handleADDLOCATION((rva, self.psize, v_const.LOC_POINTER, None))

    def _handleADDMODULE(self, einfo):
        raise warnings.DeprecationWarning('AddModule is pending deletion')

    def _handleDELMODULE(self, einfo):
        raise warnings.DeprecationWarning('DelModule is pending deletion')

    def _handleADDFMODULE(self, einfo):
        raise warnings.DeprecationWarning('AddFModule is pending deletion')

    def _handleDELFMODULE(self, einfo):
        raise warnings.DeprecationWarning('AddFModule is pending deletion')

    def _handleADDFUNCTION(self, einfo):
        va, meta = einfo
        self._initFunction(va)

        # node = self._call_graph.addNode( nid=va, repr=self.getName( va ) ) #, color='#00ff00' )
        # node = self._call_graph.getFunctionNode(va, repr=self.getName( va ) )
        node = self._call_graph.getFunctionNode(va)
        self._call_graph.setNodeProp(node, 'repr', self.getName(va))

        # Tell the codeflow subsystem about this one!
        calls_from = meta.get('CallsFrom')
        self.cfctx.addFunctionDef(va, calls_from)

        self.funcmeta[va] = meta

        for name, value in meta.items():
            mcbname = "_fmcb_%s" % name.split(':')[0]
            mcb = getattr(self, mcbname, None)
            if mcb is not None:
                mcb(va, name, value)

    def _handleDELFUNCTION(self, einfo):
        # clear funcmeta, func_args, codeblocks_by_funcva, update codeblocks, blockgraph, locations, etc...
        fva = einfo

        # not every codeblock identifying as this function is stored in funcmeta
        for cb in self.getCodeBlocks():
            if cb[v_const.CB_FUNCVA] == fva:
                self._handleDELCODEBLOCK(cb)

        self.funcmeta.pop(fva)
        self.func_args.pop(fva, None)
        self.codeblocks_by_funcva.pop(fva)
        node = self._call_graph.getNode(fva)
        self._call_graph.delNode(node)
        self.cfctx.flushFunction(fva)

        # FIXME: do we want to now seek the function we *should* be in?
        # if xrefs_to, look for non-PROC code xrefs and take their function
        # if the previous instruction falls through, take its function
        # run codeblock analysis on that function to reassociate the blocks
        # with that function

    def _handleSETFUNCMETA(self, einfo):
        funcva, name, value = einfo
        m = self.funcmeta.get(funcva)
        if m is not None:
            m[name] = value
        mcbname = "_fmcb_%s" % name.split(':')[0]
        mcb = getattr(self, mcbname, None)
        if mcb is not None:
            mcb(funcva, name, value)

    def _handleADDCODEBLOCK(self, einfo):
        va, size, funcva = einfo
        self.blockmap.setMapLookup(va, size, einfo)
        self.codeblocks_by_funcva.get(funcva).append(einfo)
        self.codeblocks.append(einfo)

    def _handleDELCODEBLOCK(self, cb):
        va, size, funcva = cb
        self.codeblocks.remove(cb)
        self.codeblocks_by_funcva.get(cb[v_const.CB_FUNCVA]).remove(cb)
        self.blockmap.setMapLookup(va, size, None)

    def _handleADDXREF(self, einfo):
        fromva, tova, reftype, rflags = einfo
        xr_to = self.xrefs_by_to.get(tova, None)
        xr_from = self.xrefs_by_from.get(fromva, None)
        if xr_to is None:
            xr_to = []
            self.xrefs_by_to[tova] = xr_to

        if xr_from is None:
            xr_from = []
            self.xrefs_by_from[fromva] = xr_from

        if einfo not in xr_to:  # Just check one for now
            xr_to.append(einfo)
            xr_from.append(einfo)
            self.xrefs.append(einfo)

    def _handleDELXREF(self, einfo):
        fromva, tova, reftype, refflags = einfo
        self.xrefs_by_to[tova].remove(einfo)
        self.xrefs_by_from[fromva].remove(einfo)

    def _handleSETNAME(self, einfo):
        va, name = einfo
        if name is None:
            oldname = self.name_by_va.pop(va, None)
            self.va_by_name.pop(oldname, None)

        else:
            curname = self.name_by_va.get(va)
            if curname is None:
                logger.debug('replacing 0x%x: %r -> %r', va, curname, name)
                self.va_by_name.pop(curname)

            self.va_by_name[name] = va
            self.name_by_va[va] = name

        if self.isFunction(va):
            fnode = self._call_graph.getFunctionNode(va)
            self._call_graph.setNodeProp(fnode, 'repr', name)

    def _handleADDMMAP(self, einfo):
        va, perms, fname, mbytes = einfo
        e_mem.MemoryObject.addMemoryMap(self, va, perms, fname, mbytes)

        blen = len(mbytes)
        self.locmap.initMapLookup(va, blen)
        self.blockmap.initMapLookup(va, blen)

        # On loading a new memory map, we need to crush a few
        # transmeta items...
        self.transmeta.pop('findPointers', None)

    def _handleADDEXPORT(self, einfo):
        va, etype, name, filename = einfo
        self.exports.append(einfo)
        self.exports_by_va[va] = einfo

    def _handleSETMETA(self, einfo):
        name, value = einfo
        # See if there's a callback handler for this meta set.
        # For "meta namespaces" use the first part to find the
        # callback name....
        mcbname = "_mcb_%s" % name.split(':')[0]
        mcb = getattr(self, mcbname, None)
        if mcb is None:
            mcb(name, value)
        self.metadata[name] = value

    def _handleCOMMENT(self, einfo):
        va, comment = einfo
        if comment is None:
            self.comments.pop(va, None)
        else:
            self.comments[va] = comment

    def _handleADDFILE(self, einfo):
        normname, imagebase, md5sum = einfo
        self.filemeta[normname] = {
            "md5sum": md5sum,
            "imagebase": imagebase,
        }

    def _handleSETFILEMETA(self, einfo):
        fname, key, value = einfo
        self.filemeta.get(fname)[key] = value

    def _handleADDCOLOR(self, coltup):
        mapname, colmap = coltup
        self.colormaps[mapname] = colmap

    def _handleDELCOLOR(self, mapname):
        self.colormaps.pop(mapname)

    def _handleADDVASET(self, argtup):
        name, defs, rows = argtup
        # NOTE: legacy translation for vaset column types...
        defs = [(cname, vaset_xlate.get(ctype, ctype))
                for (cname, ctype) in defs]
        self.vasetdefs[name] = defs
        vals = {}
        for row in rows:
            vals[row[0]] = row
        self.vasets[name] = vals

    def _handleDELVASET(self, setname):
        self.vasetdefs.pop(setname)
        self.vasets.pop(setname)

    def _handleADDFREF(self, frtup):
        va, idx, val = frtup
        self.frefs[(va, idx)] = val

    def _handleDELFREF(self, frtup):
        va, idx, val = frtup
        self.frefs.pop((va, idx), None)

    def _handleSETVASETROW(self, argtup):
        name, row = argtup
        self.vasets[name][row[0]] = row

    def _handleDELVASETROW(self, argtup):
        name, va = argtup
        self.vasets[name].pop(va, None)

    def _handleCHAT(self, msgtup):
        # FIXME make a GUI window for this...
        user, msg = msgtup
        self.vprint('%s: %s' % (user, msg))

    def _handleSYMHINT(self, msgtup):
        va, idx, hint = msgtup
        if hint is None:
            self.symhints.pop((va, idx), None)
        else:
            self.symhints[(va, idx)] = hint

    def _handleSETFUNCARGS(self, einfo):
        fva, args = einfo
        self.func_args[fva] = args

    def _handleAUTOANALFIN(self, einfo):
        '''
        This event is more for the storage subsystem than anything else. It
        marks the end of autoanalysis.  Any event beyond this is due to the
        end user or analysis modules they've executed.
        '''
        pass

    def _initEventHandlers(self):
        self.ehand = [None for x in range(v_const.WorkspaceEvents)]
        self.ehand[v_const.VWE_ADDLOCATION] = self._handleADDLOCATION
        self.ehand[v_const.VWE_DELLOCATION] = self._handleDELLOCATION
        self.ehand[v_const.VWE_ADDSEGMENT] = self._handleADDSEGMENT
        self.ehand[v_const.VWE_DELSEGMENT] = None
        self.ehand[v_const.VWE_ADDRELOC] = self._handleADDRELOC
        self.ehand[v_const.VWE_DELRELOC] = None
        self.ehand[v_const.VWE_ADDMODULE] = self._handleADDMODULE
        self.ehand[v_const.VWE_DELMODULE] = self._handleDELMODULE
        self.ehand[v_const.VWE_ADDFMODULE] = self._handleADDFMODULE
        self.ehand[v_const.VWE_DELFMODULE] = self._handleDELFMODULE
        self.ehand[v_const.VWE_ADDFUNCTION] = self._handleADDFUNCTION
        self.ehand[v_const.VWE_DELFUNCTION] = self._handleDELFUNCTION
        self.ehand[v_const.VWE_SETFUNCARGS] = self._handleSETFUNCARGS
        self.ehand[v_const.VWE_SETFUNCMETA] = self._handleSETFUNCMETA
        self.ehand[v_const.VWE_ADDCODEBLOCK] = self._handleADDCODEBLOCK
        self.ehand[v_const.VWE_DELCODEBLOCK] = self._handleDELCODEBLOCK
        self.ehand[v_const.VWE_ADDXREF] = self._handleADDXREF
        self.ehand[v_const.VWE_DELXREF] = self._handleDELXREF
        self.ehand[v_const.VWE_SETNAME] = self._handleSETNAME
        self.ehand[v_const.VWE_ADDMMAP] = self._handleADDMMAP
        self.ehand[v_const.VWE_DELMMAP] = None
        self.ehand[v_const.VWE_ADDEXPORT] = self._handleADDEXPORT
        self.ehand[v_const.VWE_DELEXPORT] = None
        self.ehand[v_const.VWE_SETMETA] = self._handleSETMETA
        self.ehand[v_const.VWE_COMMENT] = self._handleCOMMENT
        self.ehand[v_const.VWE_ADDFILE] = self._handleADDFILE
        self.ehand[v_const.VWE_DELFILE] = None
        self.ehand[v_const.VWE_SETFILEMETA] = self._handleSETFILEMETA
        self.ehand[v_const.VWE_ADDCOLOR] = self._handleADDCOLOR
        self.ehand[v_const.VWE_DELCOLOR] = self._handleDELCOLOR
        self.ehand[v_const.VWE_ADDVASET] = self._handleADDVASET
        self.ehand[v_const.VWE_DELVASET] = self._handleDELVASET
        self.ehand[v_const.VWE_SETVASETROW] = self._handleSETVASETROW
        self.ehand[v_const.VWE_DELVASETROW] = self._handleDELVASETROW
        self.ehand[v_const.VWE_ADDFREF] = self._handleADDFREF
        self.ehand[v_const.VWE_DELFREF] = self._handleDELFREF
        self.ehand[v_const.VWE_FOLLOWME] = self._handleFOLLOWME
        self.ehand[v_const.VWE_CHAT] = self._handleCHAT
        self.ehand[v_const.VWE_SYMHINT] = self._handleSYMHINT
        self.ehand[v_const.VWE_AUTOANALFIN] = self._handleAUTOANALFIN

        self.thand = [None for x in range(v_const.VTE_MAX)]
        self.thand[v_const.VTE_IAMLEADER] = self._handleIAMLEADER
        self.thand[v_const.VTE_FOLLOWME] = self._handleFOLLOWME

    def _handleIAMLEADER(self, event, einfo):
        user, follow = einfo
        self.vprint(f'{user} invites everyone to follow "{follow}"')

    def _handleFOLLOWME(self, event, einfo):
        # workspace has nothing to do...
        pass

    def _fireEvent(self, event, einfo, local=False, skip=None):
        '''
        Fire an event down the hole.  "local" specifies that this is
        being called on a client (self.server != None) but we got it
        from the server in the first place so no need to send it back.

        skip is used to tell the server to bypass our channelid when
        putting the event into channel queues (we took care of our own).
        '''

        try:
            if event & v_const.VTE_MASK:
                return self._fireTransEvent(event, einfo)

            # Do our main event processing
            self.ehand[event](einfo)

            # If we're supposed to call a server, do that.
            if self.server is not None and not local:
                self.server._fireEvent(event, einfo, skip=self.rchan)

            # FIXME perhaps we should only process events *via* our server
            # if we have one? Just to confirm it works before we apply it...
            self._event_list.append((event, einfo))

            for idx, q in self.chan_lookup.items():
                if idx == skip:
                    continue
                try:
                    q.put_nowait((event, einfo))
                except queue.Full:
                    logging.warning("FULL QUEUE DO SOMETHING")

        except Exception:
            traceback.print_exc()

    def _fireTransEvent(self, event, einfo):
        for q in self.chan_lookup.values():
            q.put((event, einfo))
        return self.thand[event ^ v_const.VTE_MASK](event, einfo)

    def _initFunction(self, funcva):
        # Internal function to initialize all datastructures necessary for
        # a function, but only if they haven't been done already.
        if self.funcmeta.get(funcva) is None:
            self.funcmeta[funcva] = {}  # His metadata
            self.codeblocks_by_funcva[funcva] = []  # Init code block list

    def getEndian(self):
        return self.bigend

    def setEndian(self, endian):
        self.bigend = endian
        for arch in self.imem_archs:
            arch.setEndian(self.bigend)

        if self.arch is not None:
            self.arch.setEndian(self.bigend)

#################################################################
#
#  setMeta key callbacks
#

    def _mcb_Architecture(self, name, value):
        # This is for legacy stuff...
        self.arch = envi.getArchModule(value)
        self.psize = self.arch.getPointerSize()

        archid = envi.getArchByName(value)
        self.setMemArchitecture(archid)

        # Default calling convention for architecture
        # This will be superceded by Platform and Parser settings
        defcall = self.arch.getArchDefaultCall()
        if defcall:
            self.setMeta('DefaultCall', defcall)

    def _mcb_bigend(self, name, value):
        self.setEndian(bool(value))

    def _mcb_Platform(self, name, value):
        # Default calling convention for platform
        # This supercedes Architecture's setting and should make
        # parser settings obsolete
        defcall = self.arch.getPlatDefaultCall(value)
        if defcall:
            self.setMeta('DefaultCall', defcall)

    def _mcb_ustruct(self, name, ssrc):
        # All meta values in the "ustruct" namespace are user defined
        # structure defintions in C.
        sname = name.split(':')[1]
        ctor = vs_cparse.ctorFromCSource(ssrc)
        self.vsbuilder.addVStructCtor(sname, ctor)

    def _mcb_WorkspaceServer(self, name, wshost):
        self.vprint(f'Workspace was Saved to Server: {wshost}')
        self.vprint(
            '(You must close this local copy and work from the server to stay in sync.)')

    def _fmcb_Thunk(self, funcva, th, thunkname):
        # If the function being made a thunk is registered
        # in NoReturnApis, update codeflow...
        if self.getMeta('NoReturnApis').get(thunkname.lower()):
            self.cfctx.addNoReturnAddr(funcva)

    def _fmcb_CallsFrom(self, funcva, th, callsfrom):
        for va in callsfrom:
            f2va = self.getFunction(va)
            if f2va is not None:
                self._call_graph.getCallEdge(funcva, f2va)

    def _fmcb_LocalSymbol(self, fva, mname, locsym):
        fva, spdelta, symtype, syminfo = locsym
        self.localsyms[fva][spdelta] = locsym


def trackDynBranches(cfctx, op, vw, bflags, branches):
    '''
    track dynamic branches
    '''
    # FIXME: do we want to filter anything out?
    #  jmp edx
    #  jmp dword [ebx + 68]
    #  call eax
    #  call dword [ebx + eax * 4 - 228]

    # if we have any xrefs from here, we have already been analyzed.  nevermind.
    if len(vw.getXrefsFrom(op.va)):
        return

    if vw.verbose:
        logging.info("Dynamic Branch found at 0x%x    %s" % (op.va, op))
    vw.setVaSetRow('DynamicBranches', (op.va, repr(op), bflags))


class VivCodeFlowContext(e_codeflow.CodeFlowContext):
    def __init__(self, mem, persist=False, exptable=True, recurse=True):
        e_codeflow.CodeFlowContext.__init__(
            self, mem, persist=persist, exptable=exptable, recurse=recurse)
        self.addDynamicBranchHandler(trackDynBranches)

    def _cb_noflow(self, srcva, dstva):
        vw = self._mem
        loc = vw.getLocation(srcva)
        if loc is None:
            return

        lva, lsize, ltype, linfo = loc
        if ltype != v_const.LOC_OP:
            return

        # Update the location def for NOFALL bit
        vw.delLocation(lva)
        vw.addLocation(lva, lsize, ltype, linfo | envi.IF_NOFALL)

        vw.setVaSetRow('NoReturnCalls', (lva,))

    # NOTE: self._mem is the viv workspace...
    def _cb_opcode(self, va, op, branches):

        loc = self._mem.getLocation(va)
        if loc is None:

            # dont code flow through import calls
            branches = [br for br in branches if not self._mem.isLocType(br[0], v_const.LOC_IMPORT)]

            self._mem.makeOpcode(op.va, op=op)
            # FIXME: future home of makeOpcode branch/xref analysis
            return branches

        return ()

    def _cb_function(self, fva, fmeta):

        vw = self._mem
        if vw.isFunction(fva):
            return

        # This may be possible if an export/symbol was mistaken for
        # a function...
        if not vw.isLocType(fva, v_const.LOC_OP):
            return

        # If the function doesn't have a name, make one
        if vw.getName(fva) is None:
            vw.makeName(fva, "sub_%.8x" % fva)

        vw._fireEvent(v_const.VWE_ADDFUNCTION, (fva, fmeta))

        # Go through the function analysis modules in order
        for fmname in vw.fmodlist:
            fmod = vw.fmods.get(fmname)
            try:
                fmod.analyzeFunction(vw, fva)
            except Exception as e:
                if vw.verbose:
                    traceback.print_exc()
                vw.verbprint("Function Analysis Exception for 0x%x   %s: %s" % (
                    fva, fmod.__name__, e))
                vw.setFunctionMeta(fva, "%s fail" %
                                   fmod.__name__, traceback.format_exc())

        fname = vw.getName(fva)
        if vw.getMeta('NoReturnApis').get(fname.lower()):
            self._cf_noret[fva] = True

        if len(vw.getFunctionBlocks(fva)) == 1:
            return

        fmeta = vw.getFunctionMetaDict(fva)
        for lva in vw.getVaSetRows('NoReturnCalls'):
            va = lva[0]
            ctup = vw.getCodeBlock(va)
            if ctup and fva == ctup[2] and vw.getFunctionMeta(fva, 'BlockCount', default=0) == 1:
                self._cf_noret[fva] = True
                break

    def _cb_branchtable(self, tablebase, tableva, destva):

        if tablebase != tableva and self._mem.getXrefsTo(tableva):
            return False

        if self._mem.getLocation(tableva) is None:
            self._mem.makePointer(tableva, tova=destva, follow=False)

        return True
